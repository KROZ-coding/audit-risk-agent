"""文件名安全清洗工具 - 移除 Windows 非法字符"""
import re


_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_UNDERSCORE = re.compile(r'_{2,}')


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """将任意字符串清洗为 Windows 安全文件名。

    处理规则:
    1. 替换所有 Windows 非法字符为 _
    2. 折叠连续 _ 为单个
    3. 去除首尾空白和 _
    4. 截断到 max_len 长度
    5. 空字符串回退为 "未知"
    """
    if not name:
        return "未知"
    safe = _ILLEGAL_CHARS.sub('_', str(name))
    safe = _MULTI_UNDERSCORE.sub('_', safe)
    safe = safe.strip(' ._')
    if len(safe) > max_len:
        safe = safe[:max_len].rstrip('._')
    return safe or "未知"
