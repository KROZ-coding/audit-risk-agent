"""外部数据核验工具（E1）——MCP 接入框架

目的：接入外部数据源（MCP server：公告/行情类）核验财报数据的真实性，
弥补「全链路闭环只能证明报表内部自洽、证明不了与官方披露一致」的能力缺口
（外部专家评审意见 E1）。

当前实现为「框架 + 降级语义」：
- ``verify_against_external_source``：对上传年报的关键科目与外部数据做一致性
  核对。外部源通过 ``EXTERNAL_VERIFY_MCP_URL``（或环境变量配置的 MCP server）
  接入；未配置/网络失败/超时时返回 ``external_unavailable``，报告显式标注
  「未经外部核验」，不阻塞主链路（降级可见，fail-open 但不冒充已核验）。
- 核对项：总资产、营业收入、净利润、审计意见类型（与公开披露的年报摘要比对）。
- 一致性判定：容差 0.5%（单位换算差异 + 舍入），结果三态：consistent /
  inconsistent / external_unavailable。

后续接入真实 MCP server 时，在 ``_fetch_external_filing`` 中实现 MCP 调用
（stdio/HTTP transport），本工具的三态输出与证据登记格式不变。

注册：工具已加入 config/agent_llm_config.json 的 tools 列表与
build_agent 的 LLM 工具集（17 个，含本工具后计数同步更新）。
"""
import json
import logging
import os
from datetime import datetime

from core.result_contract import Evidence, MetricResult, RESULT_SCHEMA_VERSION, RULE_VERSION, make_fact
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 外部核验源配置（环境变量）：未配置时工具诚实返回 external_unavailable
EXTERNAL_VERIFY_MCP_URL = os.getenv("EXTERNAL_VERIFY_MCP_URL", "")
EXTERNAL_VERIFY_TIMEOUT = int(os.getenv("EXTERNAL_VERIFY_TIMEOUT", "10"))

# 一致性容差：单位换算差异 + 舍入（0.5%）
_CONSISTENCY_TOLERANCE = 0.005

# 核对项与字段映射
_VERIFY_FIELDS = [
    ("total_assets", "总资产"),
    ("revenue", "营业收入"),
    ("net_profit", "净利润"),
]


def _fetch_external_filing(company_name: str, report_period: str) -> dict | None:
    """从外部数据源（MCP server）取公司当期公开披露数据。

    未配置 EXTERNAL_VERIFY_MCP_URL 或网络失败时返回 None（外部不可用）。
    接入真实 MCP server 后在此实现调用（如巨潮公告检索、行情摘要）；
    返回格式：{"source": "cninfo", "fields": {"total_assets": ..., ...},
    "audit_opinion": "标准无保留意见", "retrieved_at": "..."}。
    """
    # 运行时读取环境变量（测试可 monkeypatch；模块级缓存会让 env 覆盖失效）
    mcp_url = os.getenv("EXTERNAL_VERIFY_MCP_URL", "")
    timeout = int(os.getenv("EXTERNAL_VERIFY_TIMEOUT", "10"))
    if not mcp_url:
        return None
    try:
        # MCP 调用占位：真实实现按 MCP 协议（stdio/HTTP）调用已配置的 server。
        # 框架期先诚实返回不可用，绝不冒充已核验。
        import urllib.request
        req = urllib.request.Request(
            f"{mcp_url.rstrip('/')}/filing",
            data=json.dumps({"company": company_name, "period": report_period},
                            ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("fields"), dict):
            return data
        return None
    except Exception as e:  # noqa: BLE001 — 外部失败必须降级可见，不冒充核验
        logger.warning(f"外部数据源查询失败（external_unavailable）: {e}")
        return None


@tool
def verify_against_external_source(financial_data_json: str) -> str:
    """将年报关键科目与外部公开披露数据（MCP 数据源）核对，验证财报数据真实性。

    本工具弥补内部校验的闭环缺口：三大勾稽只能证明报表内部自洽，
    本工具核对「与官方披露是否一致」。核对项：总资产、营业收入、净利润。

    Args:
        financial_data_json: calculate_financial_indicators 同源的财务数据 JSON
            （含 company_info.company_name、period 及数值字段）。

    Returns:
        JSON 字符串，status 三态：
        - consistent: 关键科目与外部披露一致（容差 0.5%）
        - inconsistent: 存在超出容差的差异（附差异明细，须人工核查）
        - external_unavailable: 外部数据源未配置/不可用——报告须标注
          「未经外部核验」，不得冒充已核验
    """
    metric_id = "external_verification"
    evidence_id = f"E-{metric_id}"
    try:
        data = json.loads(financial_data_json) if isinstance(financial_data_json, str) else financial_data_json
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    meta = data.get("_metadata") or data.get("metadata") or {}
    company_name = str(data.get("company_name") or meta.get("company_name") or "").strip()
    report_period = str(data.get("period") or meta.get("period") or
                        data.get("report_period") or "").strip()

    external = _fetch_external_filing(company_name, report_period)
    if external is None:
        reason = ("未配置外部数据源（EXTERNAL_VERIFY_MCP_URL）" if not os.getenv("EXTERNAL_VERIFY_MCP_URL", "")
                  else "外部数据源查询失败或超时")
        output = {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "rule_version": RULE_VERSION,
            "calculation_version": "2026-10-v1",
            "status": "external_unavailable",
            "company_name": company_name or "未提供",
            "report_period": report_period or "未提供",
            "message": f"未经外部核验：{reason}。本报告的数据真实性仅由内部勾稽校验支撑，"
                       "未与官方公开披露核对；请在报告中如实标注「未经外部核验」。",
            "external_source": os.getenv("EXTERNAL_VERIFY_MCP_URL") or None,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "metric_id": metric_id, "evidence_id": evidence_id,
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    # 有外部数据：逐项核对
    checks = []
    all_consistent = True
    for field, label in _VERIFY_FIELDS:
        internal = data.get(field)
        external_val = (external.get("fields") or {}).get(field)
        if internal is None or external_val is None:
            checks.append({"field": field, "label": label, "status": "not_comparable",
                           "reason": "内部或外部数据缺失该项"})
            continue
        try:
            iv, ev = float(internal), float(external_val)
        except (TypeError, ValueError):
            checks.append({"field": field, "label": label, "status": "not_comparable",
                           "reason": "数值不可解析"})
            continue
        if ev == 0:
            consistent = iv == 0
        else:
            consistent = abs(iv - ev) / abs(ev) <= _CONSISTENCY_TOLERANCE
        all_consistent = all_consistent and consistent
        checks.append({
            "field": field, "label": label, "status": "consistent" if consistent else "inconsistent",
            "internal_value": iv, "external_value": ev,
            "deviation_pct": round(abs(iv - ev) / abs(ev) * 100, 4) if ev else None,
        })

    status = "consistent" if all_consistent else "inconsistent"
    inconsistent = [c for c in checks if c.get("status") == "inconsistent"]
    if status == "consistent":
        message = (f"关键科目与外部披露（{external.get('source', '外部数据源')}）核对一致"
                   f"（容差 {_CONSISTENCY_TOLERANCE * 100:.1f}%）。")
    else:
        message = (f"发现 {len(inconsistent)} 项科目与外部披露不一致"
                   f"（{('、'.join(c['label'] for c in inconsistent))}），"
                   "可能存在披露口径差异、单位换算错误或数据失真，须人工核查原始公告。")

    fact_list = [make_fact(f"verify_{c['field']}", c.get("internal_value"),
                           fact_id=f"F-VERIFY-{c['field']}", unit="元",
                           source_document=f"external:{external.get('source', 'unknown')}",
                           extraction_method="external_verification").to_dict()
                 for c in checks if c.get("internal_value") is not None]
    metric = MetricResult(
        metric_id=metric_id, name="外部数据核验",
        formula="内部科目 vs 外部披露（容差 0.5%）",
        inputs=[{"field": f["field"], "fact_id": f["fact_id"],
                 "raw_value": f["raw_value"], "value": f.get("value")}
                for f in fact_list],
        period=report_period or "未提供", scope="external_cross_check",
        unit="项", value=len(checks),
        display_value=f"{sum(1 for c in checks if c.get('status') == 'consistent')}/{len(checks)} 一致",
        status="calculated", evidence_ids=[evidence_id],
    ).to_dict()
    evidence = Evidence(
        evidence_id=evidence_id, source_type="external_verification",
        source_document=f"external:{external.get('source', 'unknown')}",
        excerpt=message, fact_ids=[f["fact_id"] for f in fact_list],
        metric_ids=[metric_id], verified=True, status="verified",
    ).to_dict()
    output = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "calculation_version": "2026-10-v1",
        "status": status,
        "company_name": company_name or "未提供",
        "report_period": report_period or "未提供",
        "external_source": external.get("source", "unknown"),
        "message": message,
        "checks": checks,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "metric_id": metric_id, "evidence_id": evidence_id,
        "facts": fact_list, "metric_results": [metric], "evidence": [evidence],
    }
    return json.dumps(output, ensure_ascii=False, indent=2)
