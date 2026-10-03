# -*- coding: utf-8 -*-
"""本地案例口径修正规则 · 示例模板（合成数据，可安全入库）。

用法：复制为 config/local_case_overrides.py（该路径已列入 .gitignore），
把其中的合成占位内容替换为针对具体真实年报的修正规则。agent.py 启动时
自动加载该模块；未提供时 _normalize_source_bound_text 仅做结构递归、不改写文案。

模块契约（两个入口，缺一不可）：
- normalize_source_bound_str(value: str) -> str
    字符串口径清洗：对 LLM 输出中可由源报告确定的固定事实表述做确定性改写
    （如持股口径、跨期比较措辞、内部参考值来源标注）。必须保持幂等。
- adjust_company_specific_risks(value: dict) -> None
    公司特定风险的等级复核：基于源报告已明确披露的事实，把不能成立的
    风险定性降级为一般/暂定关注（保留原等级留痕）。

真实规则属于本地私有数据：不要把任何真实公司名、股票代码或报表数字
写进将提交到版本库的文件。
"""
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
    if not isinstance(value, str) or not value:
        return value
    value = value.replace("甲集团持股比例55.00%（含间接持有H股）", CANONICAL_EQUITY)
    value = value.replace(
        "应收账款账面余额较上年末激增12.00%，与营收下降3.00%显著背离",
        CANONICAL_RECEIVABLE,
    )
    value = re.sub(r"(应收账款(?:账面余额|余额))同比", r"\1较上年末", value)
    value = value.replace("关联采购占同类交易9.99%，未超过30%内部筛查参考值",
                          CANONICAL_RELATED_PARTY)
    return value


def adjust_company_specific_risks(value):
    company_info = value.get("company_info")
    if (isinstance(company_info, dict)
            and "甲集团" in str(company_info.get("company_name", ""))):
        for risk in value.get("risk_details", []) or []:
            if not isinstance(risk, dict) or "担保" not in str(risk.get("title", "")):
                continue
            risk_text = " ".join(str(risk.get(k, "")) for k in ("title", "evidence", "data_analysis"))
            if ("不存在" in risk_text and "关联方担保" in risk_text
                    and not any(marker in risk_text for marker in ("违规", "未披露", "披露遗漏"))
                    and risk.get("level") == "重要"):
                risk["original_level"] = risk.get("original_level") or risk.get("level")
                risk["level"] = "一般"
                risk["verification_status"] = "暂定关注"
                risk["pending_reason"] = "担保总额及分类已披露；关联方担保不存在的管理层声明和被担保主体范围仍待独立核查"
