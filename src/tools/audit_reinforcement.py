# -*- coding: utf-8 -*-
"""审计补强五项字段：涉及科目、适用认定、核查程序、所需材料、企业改进建议。

整改计划要求：每条相关风险分别提供企业改进建议、涉及科目、适用认定、核查程序
及所需材料；函证、监盘、控制测试、截止测试等只列待执行建议，不宣称已经实施。

LLM 输出不稳定且各条目粒度不一，故本模块承担三件事：

1. **归一化**：字符串、列表、多行文本、字典统一为去重后的字符串列表；
2. **确定性兜底**：缺项按风险维度与科目关键词生成模板建议，并记录来源
   （``llm`` / ``llm+template`` / ``template``），供人工分辨哪些是模型输出、
   哪些是系统模板，不把模板冒充为分析结论；
3. **统一口径**：PDF 与 Excel 共用同一字段顺序与文案，避免两份产物不一致。

模板文案一律以「（待执行）」结尾，明确其为拟执行建议而非已实施程序。
"""

from __future__ import annotations

import re

# 字段顺序即 PDF/Excel 的展示顺序，固定不随缺项漂移
REINFORCEMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("involved_accounts", "涉及科目"),
    ("assertions", "适用认定"),
    ("audit_procedures", "核查程序"),
    ("required_materials", "所需材料"),
    ("improvement_suggestions", "企业改进建议"),
)

# 短枚举型字段：允许按顿号/逗号拆分为多项（长句程序描述不拆）
_INLINE_SPLIT_KEYS = {"involved_accounts", "assertions"}

# 科目关键词：按长度降序，长词（经营现金流）先于短词（现金流）命中
_SUBJECT_WORDS = tuple(sorted(
    ("应收账款", "经营现金流", "未分配利润", "货币资金", "商誉", "存货", "收入",
     "现金流", "关联", "资产负债", "净利润", "负债", "资产"),
    key=len, reverse=True))

# ── 维度级默认（兜底模板）──
_ASSERTIONS_BY_DIM = {
    "financial_misstatement": ("存在", "完整性", "准确性", "计价与分摊"),
    "data_reliability": ("完整性", "准确性"),
    "related_party": ("完整性", "准确性", "披露"),
    "disclosure_compliance": ("披露", "完整性"),
    "going_concern": ("持续经营假设适当性", "计价与分摊"),
    "regulatory_penalty": ("合规性", "披露"),
}

_PROCEDURES_BY_DIM = {
    "financial_misstatement": (
        "取得相关科目明细表，复核其完整性与准确性（待执行）",
        "选取样本执行细节测试，必要时实施函证（待执行）",
    ),
    "data_reliability": (
        "重新执行勾稽计算，并核对原始报表页码与单位口径（待执行）",
        "向管理层获取数据口径说明，复核期间与合并范围（待执行）",
    ),
    "related_party": (
        "取得关联方清单及关联交易明细，核对完整性（待执行）",
        "检查关联交易定价政策与审批记录，评价定价公允性（待执行）",
    ),
    "disclosure_compliance": (
        "将年报披露内容与适用披露规则逐项比对（待执行）",
        "核对重大事项披露时点与临时公告的一致性（待执行）",
    ),
    "going_concern": (
        "取得管理层持续经营能力评估，复核其关键假设（待执行）",
        "复核债务到期安排、期后事项与现金流预测（待执行）",
    ),
    "regulatory_penalty": (
        "查询监管机构公开的处罚与监管措施记录（待执行）",
        "评价相关事项对财务报表及披露的影响（待执行）",
    ),
}

_MATERIALS_BY_DIM = {
    "financial_misstatement": ("相关科目明细账及总账", "原始凭证与合同", "上期比较数据"),
    "data_reliability": ("年报原始报表页", "数据提取底稿", "单位与口径说明"),
    "related_party": ("关联方清单", "关联交易明细与定价政策", "审批文件"),
    "disclosure_compliance": ("年报披露全文", "适用披露规则文本", "临时公告与问询回复"),
    "going_concern": ("持续经营能力评估文件", "债务到期表与授信文件", "期后事项说明"),
    "regulatory_penalty": ("监管公开信息查询记录", "公司整改说明"),
}

_IMPROVEMENTS_BY_DIM = {
    "financial_misstatement": ("完善相关科目的核算与复核流程，留存可核验的计算过程",),
    "data_reliability": ("统一数据口径与报表单位，建立勾稽自查清单",),
    "related_party": ("完善关联方识别，规范关联交易审批与定价留档",),
    "disclosure_compliance": ("建立披露清单与复核机制，明确披露时点责任人",),
    "going_concern": ("制定流动性改善与债务滚动安排，定期更新现金流预测",),
    "regulatory_penalty": ("针对已受处罚事项建立整改台账与合规培训机制",),
}

# 维度未知时的中性兜底（不猜测具体认定，交人工确认）
_UNKNOWN_DIM_TEXT = "待人工确认"

# ── 科目级追加项 ──
_SUBJECT_PROCEDURES = (
    ("应收账款", ("结合账龄分析复核坏账准备计提（待执行）",
                  "选取样本执行函证，未回函的实施替代测试（待执行）")),
    ("存货", ("对存货实施监盘，并复核跌价准备测算（待执行）",)),
    ("收入", ("执行收入截止性测试，检查是否存在跨期确认（待执行）",)),
    ("商誉", ("复核商誉减值测试的关键假设、折现率及其参数来源（待执行）",)),
    ("未分配利润", ("重新计算未分配利润变动与归母净利润、分红的勾稽（待执行）",)),
    ("货币资金", ("核对银行对账单与函证回函，检查资金受限情况（待执行）",)),
    ("关联", ("取得关联方清单，核对交易定价与审批（待执行）",)),
)

_SUBJECT_MATERIALS = (
    ("应收账款", ("应收账款账龄分析表", "坏账准备计提明细", "主要客户合同")),
    ("存货", ("存货明细表与盘点记录", "存货跌价准备测算表")),
    ("收入", ("主要销售合同与验收单据", "收入确认政策")),
    ("商誉", ("商誉减值测试底稿", "折现率与关键假设依据")),
    ("未分配利润", ("未分配利润变动表", "分红决议")),
    ("货币资金", ("银行对账单与函证回函", "资金受限情况说明")),
)

_SUBJECT_ACCOUNTS = (
    ("应收账款", "应收账款"),
    ("存货", "存货"),
    ("商誉", "商誉"),
    ("收入", "营业收入"),
    ("未分配利润", "未分配利润"),
    ("货币资金", "货币资金"),
    ("经营现金流", "经营活动现金流量"),
    ("现金流", "现金流量"),
    ("资产负债", "资产与负债项目"),
    ("净利润", "净利润"),
    ("关联", "关联方交易"),
)


def _dedupe(items: list[str]) -> list[str]:
    """保序去重并清理项首尾的项目符号/标点。"""
    seen = set()
    result = []
    for item in items:
        text = str(item).strip().strip("-•*· \t")
        text = text.rstrip("。;；,，")
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _as_list(value, *, split_inline: bool = False) -> list[str]:
    """把任意形态的字段值归一为字符串列表。

    Args:
        value: 字符串 / 列表 / 元组 / 字典（LLM 偶发以 ``{"1": "..."}`` 输出）
        split_inline: 为真时额外按顿号、逗号拆分（用于短枚举字段）
    """
    if value is None:
        return []
    if isinstance(value, dict):
        return _as_list(list(value.values()), split_inline=split_inline)
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            items.extend(_as_list(item, split_inline=split_inline))
        return _dedupe(items)
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"[\n；;]+", text)
    if split_inline:
        parts = [piece for part in parts for piece in re.split(r"[、,，]+", part)]
    return _dedupe(parts)


def _dim_key(risk: dict) -> str:
    """风险维度归一为规范英文键；无法识别时返回空串。"""
    raw = str((risk or {}).get("dimension", "") or "").strip()
    if not raw:
        return ""
    try:
        from tools.pdf_export import _norm_dim

        normalized = str(_norm_dim(raw) or "")
    except Exception:  # noqa: BLE001 - 渲染路径不得因归一化失败而中断
        normalized = raw
    return normalized if normalized in _PROCEDURES_BY_DIM else ""


def _subject_text(risk: dict) -> str:
    """拼接用于科目匹配的文本（标题、证据、数据分析与已填科目）。"""
    parts = [str((risk or {}).get(key, "") or "") for key in
             ("title", "evidence", "data_analysis", "involved_accounts")]
    return " ".join(parts)


def derive_reinforcement(risk: dict) -> dict[str, list[str]]:
    """按维度与科目关键词生成模板补强字段（仅在对应字段缺失时使用）。"""
    dim = _dim_key(risk)
    text = _subject_text(risk)
    accounts = _dedupe([label for word, label in _SUBJECT_ACCOUNTS if word in text])
    procedures = list(_PROCEDURES_BY_DIM.get(dim, ()))
    materials = list(_MATERIALS_BY_DIM.get(dim, ()))
    for word, extra in _SUBJECT_PROCEDURES:
        if word in text:
            procedures.extend(extra)
    for word, extra in _SUBJECT_MATERIALS:
        if word in text:
            materials.extend(extra)
    if not procedures:
        procedures = ["取得相关明细与原始凭证，复核该项风险的依据（待执行）"]
    if not materials:
        materials = ["相关科目明细账及原始凭证", "管理层说明文件"]
    return {
        "involved_accounts": accounts or [_UNKNOWN_DIM_TEXT],
        "assertions": list(_ASSERTIONS_BY_DIM.get(dim, (_UNKNOWN_DIM_TEXT,))),
        "audit_procedures": _dedupe(procedures),
        "required_materials": _dedupe(materials),
        "improvement_suggestions": list(
            _IMPROVEMENTS_BY_DIM.get(dim, ("结合本项风险完善相关内部控制并留存记录",))),
    }


def get_reinforcement(risk: dict) -> dict[str, list[str]]:
    """读取五项补强字段：保留模型输出，缺项用模板补齐（不修改入参）。"""
    values: dict[str, list[str]] = {}
    missing = False
    for key, _label in REINFORCEMENT_FIELDS:
        items = _as_list((risk or {}).get(key), split_inline=key in _INLINE_SPLIT_KEYS)
        values[key] = items
        if not items:
            missing = True
    if not missing:
        return values
    derived = derive_reinforcement(risk or {})
    for key, _label in REINFORCEMENT_FIELDS:
        if not values[key]:
            values[key] = derived[key]
    return values


def ensure_reinforcement(risk: dict) -> bool:
    """就地补齐五项字段并记录来源，返回是否发生变更。

    来源取值：``llm``（五项均由模型给出）、``llm+template``（部分补齐）、
    ``template``（全部由系统模板生成），写入 ``reinforcement_source`` 供追溯。

    来源标记只允许如实"降级"：字段非空并不等于模型给出——它可能正是上一次运行
    由模板补齐的结果。若重跑时已带 template 系标记，则沿用原标记而非改标为
    ``llm``，否则模板文案会被伪装成模型结论，PDF 的模板提示行也随之消失。
    """
    if not isinstance(risk, dict):
        return False
    before = {key: risk.get(key) for key, _label in REINFORCEMENT_FIELDS}
    before_source = risk.get("reinforcement_source")
    filled_by_template = sum(
        1 for key, _label in REINFORCEMENT_FIELDS
        if not _as_list(before[key], split_inline=key in _INLINE_SPLIT_KEYS))
    values = get_reinforcement(risk)
    for key, _label in REINFORCEMENT_FIELDS:
        risk[key] = values[key]
    if filled_by_template == 0:
        source = before_source if before_source in ("template", "llm+template") else "llm"
    elif filled_by_template == len(REINFORCEMENT_FIELDS):
        source = "template"
    else:
        source = "llm+template"
    risk["reinforcement_source"] = source
    after = {key: risk.get(key) for key, _label in REINFORCEMENT_FIELDS}
    return after != before or before_source != source


def apply_reinforcement(report_obj: dict) -> int:
    """为台账中每条风险补齐五项字段，返回发生变更的条目数。"""
    details = (report_obj or {}).get("risk_details")
    if not isinstance(details, list):
        return 0
    changed = 0
    for risk in details:
        if ensure_reinforcement(risk):
            changed += 1
    return changed


def reinforcement_cell(risk: dict, key: str, separator: str = "；") -> str:
    """取单项字段的单元格文本（Excel 用），多值以分隔符连接。"""
    values = get_reinforcement(risk)
    return separator.join(values.get(key, []))


def reinforcement_items(risk: dict) -> list[tuple[str, list[str]]]:
    """按固定顺序返回 (标签, 取值列表)，供 PDF 逐项渲染。"""
    values = get_reinforcement(risk)
    return [(label, values[key]) for key, label in REINFORCEMENT_FIELDS]
