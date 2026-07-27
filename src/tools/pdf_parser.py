"""PDF 年报解析工具 - 从 PDF 文件中提取文本内容

本模块是审计分析流程的第一步，负责将上市公司年报 PDF 转化为可供 LLM 分析的纯文本。

支持的文件来源：
- 本地绝对路径（如 /data/reports/annual_report.pdf）
- 本地相对路径（相对于工作目录 COZE_WORKSPACE_PATH）
- 远程 URL（自动下载到临时文件后解析）

使用 pypdf 库进行 PDF 文本提取，提取结果按页分隔并附带页码标记。
为防止 token 溢出，提取文本超过 MAX_TEXT_LENGTH（20 万字符）时自动截断。
"""
import os
import ipaddress
import socket
import logging
import tempfile
import requests
from urllib.parse import urlparse, urljoin
from langchain_core.tools import tool
from pypdf import PdfReader

logger = logging.getLogger(__name__)

# 文本提取最大长度（字符数），超过此长度自动截断，防止 LLM 上下文溢出
MAX_TEXT_LENGTH = 200_000
# 下载大小上限（100MB），防止超大远程文件耗尽磁盘/内存
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024


def _assert_public_host(url: str) -> None:
    """SSRF 防护：解析 URL 主机并拒绝内网/回环/链路本地/云元数据等非公网地址。

    对主机名做 DNS 解析后逐个校验解出的 IP（兼顧 DNS 重绑定与多 A 记录），
    任一解析结果落入私有/保留段即拒绝。

    Raises:
        ValueError: 主机为空、无法解析或解析到非公网地址时。
    """
    host = (urlparse(url).hostname or "").strip()
    if not host:
        raise ValueError("URL 缺失合法主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"无法解析主机名: {host}") from e
    for info in infos:
        ip_str = info[4][0]
        ip = ipaddress.ip_address(ip_str)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"拒绝访问非公网地址（SSRF 防护）: {host} -> {ip_str}")


def _download_url_to_local(url: str) -> str:
    """将远程 URL 文件下载到本地临时目录，返回临时文件的本地路径。

    处理流程：
    1. 从 URL 中解析文件扩展名（缺省为 .pdf）
    2. 创建临时文件并写入下载内容
    3. 返回临时文件路径供后续 PdfReader 读取

    Args:
        url: PDF 文件的完整 HTTP/HTTPS URL

    Returns:
        下载后的本地临时文件路径

    Raises:
        requests.HTTPError: HTTP 请求失败（状态码非 2xx）时抛出
    """
    # 从 URL 路径中提取文件扩展名，无扩展名时默认 .pdf
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1] or ".pdf"
    # 创建临时文件（系统自动清理），保持原始扩展名
    fd, local_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    # SSRF 防护 + 保留合法重定向：手动逐跳跟随（最多 3 跳），每跳都重新校验主机为公网地址
    # （避免外部重定向绕过首跳校验指向内网/云元数据端点）；流式下载并强制大小上限
    current = url
    resp = None
    for _ in range(4):   # 首请求 + 最多 3 次重定向
        _assert_public_host(current)
        resp = requests.get(current, timeout=60, allow_redirects=False, stream=True)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                raise ValueError("远程返回重定向但缺少 Location 头")
            current = urljoin(current, loc)   # 兼容相对重定向
            continue
        break
    else:
        raise ValueError("重定向层级过多，已拒绝（SSRF 防护）")
    resp.raise_for_status()   # 非 2xx 状态码时抛出异常
    # 将下载内容分块写入临时文件，累计超限即中断
    total = 0
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_DOWNLOAD_SIZE:
                f.close()
                os.remove(local_path)
                raise ValueError(f"远程文件超过大小上限 {MAX_DOWNLOAD_SIZE // (1024*1024)}MB")
            f.write(chunk)
    return local_path


def _resolve_file_path(file_path: str) -> str:
    """统一解析文件路径，将各种输入格式归一化为本地绝对路径。

    支持三种输入格式：
    1. HTTP/HTTPS URL → 自动下载到本地临时文件
    2. 相对路径 → 拼接工作目录（COZE_WORKSPACE_PATH）生成绝对路径
    3. 绝对路径 → 直接返回

    Args:
        file_path: 原始文件路径（URL / 相对路径 / 绝对路径）

    Returns:
        本地绝对路径字符串
    """
    # 情况1：URL 输入，下载到本地临时文件
    if file_path.startswith(("http://", "https://")):
        return _download_url_to_local(file_path)
    # 情况2：相对路径，拼接工作目录后做边界校验（防路径遍历）
    workspace = os.getenv("COZE_WORKSPACE_PATH", os.path.join(os.path.dirname(__file__), "..", ".."))
    base = os.path.realpath(workspace)
    if not os.path.isabs(file_path):
        candidate = os.path.realpath(os.path.join(base, file_path))
        # 规范化后必须仍在工作目录内，否则拒绝（阻断 ../ 逆出）
        if candidate != base and not candidate.startswith(base + os.sep):
            raise ValueError(f"拒绝访问工作目录外的路径（路径遍历防护）: {file_path}")
        return candidate
    # 情况3：绝对路径——同样规范化并限定在工作目录内（防直接传绝对路径逆出）
    real = os.path.realpath(file_path)
    if real != base and not real.startswith(base + os.sep):
        raise ValueError(f"拒绝访问工作目录外的路径（路径遍历防护）: {file_path}")
    return real


@tool
def parse_pdf_report(file_path: str) -> str:
    """解析 PDF 格式的上市公司年报文件，提取全部页的文本内容。

    本工具是审计分析流程的入口工具，Agent 在接收到用户上传的年报 PDF 后，
    首先调用本工具提取全文文本，再将文本传递给后续工具进行财务指标计算和风险识别。

    处理流程：
    1. 路径归一化：支持 URL（自动下载）、相对路径（拼接工作目录）、绝对路径
    2. 文件存在性和格式校验
    3. 逐页提取文本，每页文本前添加页码标记
    4. 超长文本自动截断（上限 20 万字符），并附带截断提示
    5. 返回包含解析统计信息和全文文本的结果字符串

    Args:
        file_path: PDF 文件的路径，支持以下格式：
            - 绝对路径："/data/reports/annual_report.pdf"
            - 相对路径："reports/annual_report.pdf"（相对 COZE_WORKSPACE_PATH）
            - URL："https://example.com/annual_report.pdf"（自动下载）

    Returns:
        成功时：包含解析统计（文件名/总页数/提取页数/是否截断）和全文文本的字符串
        文件不存在时：错误提示信息
        非 PDF 格式时：错误提示信息
        提取无内容时：警告提示（可能是扫描件 PDF）
        解析异常时：错误信息
    """
    # 第一步：路径归一化（URL 自动下载、相对路径拼接工作目录）
    # SSRF/路径遍历防护拒绝时会抛 ValueError，转为友好错误提示而非崩溃
    try:
        file_path = _resolve_file_path(file_path)
    except ValueError as e:
        return f"错误：{e}"

    # 第二步：校验文件是否存在
    if not os.path.exists(file_path):
        return f"错误：文件不存在 - {file_path}"

    # 第三步：校验文件扩展名是否为 PDF
    if not file_path.lower().endswith(".pdf"):
        return f"错误：文件不是 PDF 格式 - {file_path}"

    try:
        # 第四步：使用 pypdf 逐页提取文本
        reader = PdfReader(file_path)
        total_pages = len(reader.pages)
        logger.info(f"开始解析 PDF: {file_path}, 总页数: {total_pages}")

        # 逐页提取文本，每页前添加页码标记便于 LLM 定位引用来源
        text_parts = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                text_parts.append(f"--- 第 {i + 1} 页 ---\n{page_text}")

        # 所有页面均无法提取文本，可能是扫描件或图片型 PDF
        if not text_parts:
            return "警告：PDF 文件已打开但未能提取到任何文本内容。可能是扫描件或图片型 PDF，建议使用 OCR 工具处理。"

        # 第五步：合并全文并检查长度限制
        full_text = "\n\n".join(text_parts)
        truncated = len(full_text) > MAX_TEXT_LENGTH
        # 超长文本截断，防止超出 LLM 上下文窗口导致 token 溢出
        if truncated:
            full_text = full_text[:MAX_TEXT_LENGTH]
            logger.warning(f"PDF文本过长({len(full_text)}字符)，已截断至{MAX_TEXT_LENGTH}字符")

        # 第六步：构建包含解析统计信息的结果文本
        result = (
            f"PDF 解析完成。文件名: {os.path.basename(file_path)}，"
            f"总页数: {total_pages}，成功提取 {len(text_parts)} 页文本。"
            f"{'（注意：文本已截断，后续页面未包含）' if truncated else ''}\n\n"
            f"{full_text}"
        )
        logger.info(f"PDF 解析完成，提取文本长度: {len(full_text)} 字符")
        return result

    except Exception as e:
        logger.error(f"PDF 解析失败: {e}")
        return f"错误：PDF 解析失败 - {str(e)}"
