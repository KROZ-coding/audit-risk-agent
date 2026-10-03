# -*- coding: utf-8 -*-
"""测试共享 fixture。

数据脱敏约定：针对具体真实报告的口径修正规则属于本地私有数据
（config/local_case_overrides.py，不入库）。测试用合成规则模块验证
agent 的加载/委托/幂等机制；真实规则的正确性由本地运行保障。
"""
import pytest

# 合成案例修正模块源码：仅测试机制用，不含任何真实公司数据。
_SYNTHETIC_CASE_MODULE = '''
"""合成案例修正规则（仅测试机制用）。"""
import re

CANONICAL_EQUITY = (
    "甲集团直接持股55.00%，另通过境外全资附属公司间接持股0.50%，合计约55.50%"
)
CANONICAL_RECEIVABLE = (
    "应收账款账面余额较上年末增长12.00%；营业收入同比下降3.00%。"
    "两项比较期间不同，暂不作背离判断，回款质量待核查"
)
CANONICAL_RELATED_PARTY = (
    "关联方提供产品和服务占同类交易9.99%。"
    "该比例与关联采购占营业成本的内部30%筛查参考值分母不同，不作阈值比较。"
    "定价公允性、审批程序及资金流向仍需核查。"
)


def normalize_source_bound_str(value):
    value = value.replace("甲集团持股比例55.00%（含间接持有H股）", CANONICAL_EQUITY)
    for old in (
        "应收账款账面余额较上年末激增12.00%，与营收下降3.00%显著背离",
        "应收账款账面余额较上年末增长12.00%，与营收下降3.00%显著背离",
        "应收账款账面余额较上年末增长12.00%，与营收下降方向背离，回款质量存在待核实事项",
    ):
        value = value.replace(old, CANONICAL_RECEIVABLE)
    value = value.replace(
        "应收账款账面余额较上年末增长12.00%，回款节奏与收入变动方向存在背离迹象",
        "应收账款期末账面余额较上年末增长12.00%，同口径回款与收入变动关系待核查",
    )
    value = value.replace(
        "背离约15.00个百分点，远超内部筛查参考的20个百分点阈值",
        "比较期间口径未对齐，暂不进行数值背离比较",
    )
    value = value.replace(
        "关联采购占同类交易9.99%，未超过30%内部筛查参考值",
        CANONICAL_RELATED_PARTY,
    )
    value = value.replace(
        "低于能源行业内部参考值12%",
        "行业参考值来源未核验，不作为风险定级依据",
    )
    value = re.sub(r"低于能源行业内部筛查参考值\\s*12%",
                   "低于内部筛查参考值12%（来源未核验，不作为行业基准或风险定级依据）",
                   value)
    value = re.sub(r"低于(?:内部)?筛查参考值\\s*30%",
                   "低于内部筛查参考值30%（来源未核验，不作为风险定级依据）",
                   value)
    value = re.sub(r"(应收账款(?:账面余额|余额))同比", r"\\1较上年末", value)
    value = re.sub(
        r"(综合风险评分\\s*\\d+(?:\\.\\d+)?分（(?:中低|中高|中等|低|高|极高)风险）)\\s*区间[）)]?",
        r"\\1", value)
    for _note in (
        "（来源未核验，不作为行业基准或风险定级依据）",
        "（来源未核验，不作为风险定级依据）",
    ):
        value = re.sub(r"(?:" + re.escape(_note) + r"){2,}", _note, value)
    for canonical in (CANONICAL_EQUITY, CANONICAL_RECEIVABLE, CANONICAL_RELATED_PARTY):
        duplicate = canonical + canonical
        while duplicate in value:
            value = value.replace(duplicate, canonical)
    return value


def adjust_company_specific_risks(value):
    """公司特定风险等级复核（合成规则：甲集团担保事项降为一般暂定关注）。"""
    company_info = value.get("company_info")
    if (isinstance(company_info, dict)
            and "甲集团" in str(company_info.get("company_name", ""))):
        for risk in value.get("risk_details", []) or []:
            if not isinstance(risk, dict) or "担保" not in str(risk.get("title", "")):
                continue
            risk["level"] = "一般"
'''


@pytest.fixture()
def synthetic_case_overrides(monkeypatch, tmp_path):
    """向 agents.agent 注入合成案例修正模块（替代不入库的本地真实规则）。"""
    import importlib.util

    from agents import agent as agent_mod

    path = tmp_path / "synthetic_case_overrides.py"
    path.write_text(_SYNTHETIC_CASE_MODULE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_synthetic_case_overrides", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(agent_mod, "_LOCAL_CASE", module)
    return module
