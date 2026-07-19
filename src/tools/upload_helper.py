"""对象存储上传辅助函数 - 本地模式下直接委托给 local_storage

本模块是存储层的中转代理，设计目的在于：
1. 解耦工具代码与具体存储实现：工具模块只需调用本函数，无需关心底层是本地存储还是 S3
2. 保持接口签名与 Coze 云端模式的 upload_file_to_storage 一致，便于云端/本地切换
3. 本地模式下将调用委托给 localization 模块的 local_storage，将文件保存到 local_storage/ 目录

未来如需切换到 S3/MinIO/OSS 等对象存储，只需修改本函数内部的委托目标即可。
"""
import os
import logging

logger = logging.getLogger(__name__)


def upload_file_to_storage(local_path: str, file_name: str, content_type: str, expire_seconds: int = 86400) -> str:
    """将本地文件上传到存储系统，返回可访问的 URL。

    本地模式下直接委托给 local_storage.upload_file_to_storage，将文件复制到
    local_storage/ 目录并返回 file:// 协议的本地路径。
    云端模式下可以无缝替换为 S3/MinIO 等对象存储的上传逻辑。

    接口签名与 Coze 平台的标准 upload_file_to_storage 保持一致，
    确保在本地开发环境和云端生产环境之间切换时无需修改调用方代码。

    Args:
        local_path: 本地文件的绝对路径（待上传的源文件）
        file_name: 存储中的文件名（可包含相对路径，如 "reports/xxx_审计风险报告.pdf"）
        content_type: 文件的 MIME 类型（如 "application/pdf"、"image/png"）
        expire_seconds: URL 过期时间（秒），本地模式下不生效，保留参数用于云端兼容

    Returns:
        成功时返回 file:// 开头的本地路径（本地模式）或 http:// 开头的 URL（云端模式），
        失败时返回以 "保存失败:" 开头的错误提示字符串
    """
    # 本地模式：委托给 local_storage 模块完成文件复制和路径返回
    from local_storage import upload_file_to_storage as _local_upload
    return _local_upload(local_path, file_name, content_type, expire_seconds)
