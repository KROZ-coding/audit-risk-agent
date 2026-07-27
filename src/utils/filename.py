"""文件名安全清洗工具 - 移除 Windows 非法字符；另提供 company_info 字段别名归一化"""
import re


# 非法字符表除 Windows 保留字符外还包含空白（\s 含空格/全角空格/tab）：
# 文件名带空格时，前后端所有 URL 提取正则都以空白为边界，链接会被拦腰截断
# 导致 404（实测事故：公司名「互太纺织（Pacific Textiles…）」含英文空格）。
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\s]')
_MULTI_UNDERSCORE = re.compile(r'_{2,}')

# company_info 字段别名表：LLM 输出的键名不总是严格等于模板约定（如用 name 代替
# company_name、report_period 代替 report_year），导致图表/报告文件名变成
# 「未知公司_未知」。此处按优先级逐个回退，在工具层兼容而非依赖提示词约束。
_COMPANY_KEYS = ["company_name", "name", "company", "company_full_name", "公司名称", "公司"]
_YEAR_KEYS = ["report_year", "year", "report_period", "fiscal_year", "period", "报告年度", "年度", "报告期"]


def resolve_company_year(company_info: dict) -> tuple:
    """从 company_info 字典中按别名表解析（公司名, 报告年度）。

    Args:
        company_info: 风险台账的 company_info 字段（可为 None / 非字典）

    Returns:
        (company, year) 字符串元组；未命中时 company 为空串、year 为空串
        （由调用方决定缺省文案，如文件名场景用“未知公司”）
    """
    if not isinstance(company_info, dict):
        return "", ""
    company = ""
    for k in _COMPANY_KEYS:
        v = company_info.get(k)
        if v:
            company = str(v).strip()
            break
    year = ""
    for k in _YEAR_KEYS:
        v = company_info.get(k)
        if v:
            year = str(v).strip()
            break
    return company, year


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """将任意字符串清洗为 Windows 安全文件名。

    处理规则:
    1. 替换所有 Windows 非法字符与空白（含全角空格）为 _（空格会截断 URL 提取）
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
