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


def to_roman(n: int) -> str:
    """将正整数转为大写罗马数字字符串（1-3999）。"""
    if not isinstance(n, int) or isinstance(n, bool):
        return str(n)
    if n <= 0 or n > 3999:
        return str(n)
    val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syms = ['M', 'CM', 'D', 'CD', 'C', 'XC', 'L', 'XL', 'X', 'IX', 'V', 'IV', 'I']
    result = []
    for i in range(len(val)):
        while n >= val[i]:
            result.append(syms[i])
            n -= val[i]
    return ''.join(result)


_ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
# 标准罗马数字形态（含减记法）：非标准串（如 IIII）不参与 max 统计，避免污染序号
_ROMAN_RE = re.compile(r'^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$')


def from_roman(s: str) -> int:
    """将大写罗马数字字符串反解为正整数；非标准/空串返回 0。"""
    if not s or not _ROMAN_RE.match(s):
        return 0
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN_VALUES.get(ch, 0)
        total += v if v >= prev else -v
        prev = v
    return total


def count_existing_runs(company: str, year: str = "") -> int:
    """返回当天该公司已用过的最大运行序号（无既有产物时为 0）。

    口径：解析既有文件名中的罗马数字后缀取 max（而非数文件个数）——
    一轮运行产出多个文件、部分轮次产物不全、旧格式（hex 前缀）残留都会让
    “文件数”口径跳号甚至碰撞覆盖（实测：I→VII→XIII 与 IX 重名覆盖），
    max+1 对以上情形天然免疫。匹配按 _ 分段精确比对公司段与年份段，
    避免子串污染（如“中国石油”误命中“中国石油天然气股份有限公司”）。
    自批次隔离上线后，新产物落在 local_storage/<YYYYMMDD_HHMMSS>/reports/ 与
    <...>/charts/ 时间戳子目录，旧平铺文件仍在，故此处从 local_storage 根
    递归扫描，保证同名公司跨批次序号仍严格递增不碰撞。

    Args:
        company: 清洗后的公司名（与 build_file_prefix 中 sanitize_filename 结果一致）
        year: 保留参数（历史兼容）；当前实现按公司段精确匹配后扫描罗马数字段，
              同公司不同年份同日分析的场景极少，不再按年份段隔离

    Returns:
        已用过的最大序号（0 表示首次运行）；调用方取 +1 即得下一序号
    """
    import os
    from datetime import datetime
    date_str = datetime.now().strftime("%Y%m%d")
    # 从 local_storage 根递归扫描：既覆盖平铺旧产物（reports/、charts/ 根层），
    # 也覆盖批次子目录产物（<YYYYMMDD_HHMMSS>/reports/、<...>/charts/），
    # 保证新批次文件参与序号统计。
    base_dirs = [
        os.path.join(os.path.dirname(__file__), "..", "..", "local_storage"),
    ]
    max_run = 0
    for base in base_dirs:
        if not os.path.isdir(base):
            continue
        # 递归扫描：平铺旧产物（reports/xxx.pdf）与批次子目录产物
        # （reports/<YYYYMMDD_HHMMSS>/xxx.pdf）双通道都参与序号统计，
        # 旧文件不删除，因此递归不影响既有 max+1 口径。
        for _root, _dirs, files in os.walk(base):
            for fname in files:
                if not fname.startswith(date_str + "_"):
                    continue
                stem = os.path.splitext(fname)[0]
                parts = stem.split("_")
                # 公司段必须精确匹配（parts[1]），避免子串污染。
                # 罗马数字段取其后首个标准罗马数字段：标准产物格式为
                # date_公司_[年份_]罗马_产物；趋势图为 date_公司_趋势图_罗马（无年份段），
                # 逐段扫描对两种形态均成立；旧 hex 前缀/非罗马数字残留自然被排除。
                if len(parts) < 3 or parts[1] != company:
                    continue
                num = 0
                for seg in parts[2:]:
                    num = from_roman(seg)
                    if num > 0:
                        break
                if num > max_run:
                    max_run = num
    return max_run


def build_file_prefix(report: dict, run_number: int | None = None) -> str:
    """生成统一的文件名前缀：日期_公司名_年份[_罗马数字]。

    PDF/Excel/图表三类产物共用同一前缀规则（原先三处各自实现，前缀规则一旦
    分化会导致同一分析的产物命名不统一），此处为唯一权威实现。

    末尾罗马数字后缀（I/II/III...）由 run_number 决定；若未传入则自动根据
    当天该公司已有文件数计算（count_existing_runs + 1），保证同一天多次分析
    同一公司时产物不互相覆盖且序号递增。

    Args:
        report: 含 company_info 的风险台账字典
        run_number: 可选的运行序号（1-based）；None 时自动计算

    Returns:
        格式为 "YYYYMMDD_公司名_年份[_罗马数字]" 的安全文件名前缀；无年份信息时省略年份部分
    """
    from datetime import datetime
    ci = report.get("company_info", {}) if isinstance(report, dict) else {}
    raw_company, raw_year = resolve_company_year(ci)
    company = sanitize_filename(raw_company or "未知公司")
    year = sanitize_filename(raw_year) if raw_year else ""
    date_str = datetime.now().strftime("%Y%m%d")

    # 确定运行序号：优先用显式参数 → 次选 company_info 内预注入值 → 最后自动计算。
    # 预注入值必须是正整数（LLM 可能在 company_info 里幻觉出字符串 run_number），
    # 非法时丢弃并重算，避免 to_roman 抛 TypeError。
    if run_number is None:
        injected = ci.get("run_number")
        if isinstance(injected, int) and not isinstance(injected, bool) and injected > 0:
            run_number = injected
    if run_number is None:
        existing = count_existing_runs(company, year)
        run_number = existing + 1

    roman = to_roman(run_number)
    suffix = f"_{roman}" if run_number > 0 else ""

    if year:
        return f"{date_str}_{company}_{year}{suffix}"
    return f"{date_str}_{company}{suffix}"
