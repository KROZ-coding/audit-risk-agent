"""Structured result contracts shared by calculations, review and exports.

The LLM may provide narrative, but these records are the authoritative carrier
for values used in decisions and for artifact traceability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


RESULT_SCHEMA_VERSION = "1.0"
RULE_VERSION = "2026-09-v3"
DATA_VERSION = "2026-09-v3"
SNAPSHOT_SCHEMA_VERSION = "2.0"

# 来源状态是跨网页、PDF、Excel 和图表的公共词汇。不要在渲染层自行
# 创建同义状态，否则同一个值会在不同产物中得到不同的可信度解释。
SOURCE_STATUS_VERIFIED = "verified"
SOURCE_STATUS_INCOMPLETE = "incomplete"
SOURCE_STATUS_UNVERIFIED = "unverified"
SOURCE_STATUS_DEMO_PLACEHOLDER = "demo_placeholder"
SOURCE_STATUS_LABELS = {
    SOURCE_STATUS_VERIFIED: "已核验",
    SOURCE_STATUS_INCOMPLETE: "数据源不完整，仅供参考",
    SOURCE_STATUS_UNVERIFIED: "来源未核验，仅供人工复核",
    SOURCE_STATUS_DEMO_PLACEHOLDER: "演示占位数据，不代表公司实际数据",
}


def source_status_label(status: str) -> str:
    return SOURCE_STATUS_LABELS.get(str(status or ""), "来源状态未记录")


def score_status_eligible(status: str) -> bool:
    return str(status or "") == SOURCE_STATUS_VERIFIED


PROVISIONAL_SUFFIX = "（暂定关注）"


def risk_level_label(risk: dict) -> str:
    """Keep candidate priority visibly provisional without changing its level key.

    Args:
        risk: 风险条目字典（读取 ``suggested_level`` / ``level`` /
            ``formal_status`` / ``level_status`` / ``pending_verification`` /
            ``evidence_pending``）

    Returns:
        str: 已采信且非候选等级时返回裸等级；否则返回「等级（暂定关注）」。
    """
    level = str(risk.get("suggested_level") or risk.get("level") or "待定级")
    if (risk.get("formal_status") == "accepted"
            and risk.get("level_status") != "provisional"
            and not risk.get("pending_verification") and not risk.get("evidence_pending")):
        return str(risk.get("level") or "待定级")
    return f"{level}{PROVISIONAL_SUFFIX}"


def split_risk_level_label(risk: dict) -> tuple[str, str]:
    """把风险等级标签拆成「裸等级 + 暂定后缀」，便于分别加粗与标色。

    网页 markdown 渲染器只对裸等级词（重大 / 重要 / 一般 / 高风险…）做彩色徽章
    替换；若调用方把 ``重要（暂定关注）`` 整串塞进 ``**…**``，徽章替换只会吃掉
    「重要」，括号后缀之后会残留一个游离的 ``</strong>``（实测渲染事故）。调用方
    按 ``f"**{level}**{suffix}"`` 组装即可同时保住徽章与排版。

    Args:
        risk: 风险条目字典（字段含义同 :func:`risk_level_label`）

    Returns:
        tuple[str, str]: ``(裸等级文本, 后缀文本)``；后缀为空串表示已采信正式等级。
    """
    label = risk_level_label(risk)
    if label.endswith(PROVISIONAL_SUFFIX):
        return label[: -len(PROVISIONAL_SUFFIX)], PROVISIONAL_SUFFIX
    return label, ""

# 产物状态：生成中（尚未产出，不代表失败）/ 成功 / 失败。
# 三态分开记录，避免「还没生成」被当成「生成失败」，也避免任务级只能全成或全败。
ARTIFACT_GENERATING = "generating"
ARTIFACT_SUCCESS = "success"
ARTIFACT_FAILED = "failed"

# 任务级状态：running（仍有产物生成中）/ completed（全部成功）/
# partial（部分完成，可下载已成功产物）/ failed（全部失败）/ not_run（无期望产物）。
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_PARTIAL = "partial"
TASK_FAILED = "failed"
TASK_NOT_RUN = "not_run"


def derive_task_status(manifest: list[dict]) -> str:
    """按产物清单派生任务级状态（部分完成必须可表达，不得只报成功/失败）。

    Args:
        manifest: ``ArtifactManifest.to_dict()`` 组成的列表

    Returns:
        上述 TASK_* 常量之一
    """
    items = [item for item in (manifest or []) if isinstance(item, dict)]
    if not items:
        return TASK_NOT_RUN
    statuses = [str(item.get("status", "") or "") for item in items]
    success = statuses.count(ARTIFACT_SUCCESS)
    if ARTIFACT_GENERATING in statuses:
        return TASK_RUNNING
    if success == len(statuses):
        return TASK_COMPLETED
    if success:
        return TASK_PARTIAL
    return TASK_FAILED


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


class ContractMixin:
    def to_dict(self) -> dict:
        data = _json_value(asdict(self))
        # 兼容旧版精确字段契约：新增元数据字段在未提供时不污染旧输出；
        # 一旦计算/绑定层写入，就会随结构化结果一起输出。
        optional_defaults = {
            "source_status": "", "source_note": "", "source_ref": {},
            "period_type": "", "comparability": "", "display_eligible": None,
            "score_eligible": None,
        }
        for key, default in optional_defaults.items():
            if key in data and data[key] in (default, None):
                data.pop(key, None)
        return data


@dataclass
class Fact(ContractMixin):
    fact_id: str
    field: str
    raw_value: str
    value: Any = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    point_in_time: str = ""
    scope: str = ""
    source_document: str = ""
    source_hash: str = ""
    page: str = ""
    locator: str = ""
    excerpt: str = ""
    extraction_method: str = ""
    status: str = "verified"
    source_status: str = ""
    source_note: str = ""
    source_ref: dict[str, Any] = field(default_factory=dict)
    period_type: str = ""
    comparability: str = ""
    display_eligible: bool | None = None
    score_eligible: bool | None = None


@dataclass
class Evidence(ContractMixin):
    evidence_id: str
    source_type: str
    source_document: str = ""
    source_hash: str = ""
    page: str = ""
    locator: str = ""
    excerpt: str = ""
    fact_ids: list[str] = field(default_factory=list)
    metric_ids: list[str] = field(default_factory=list)
    verified: bool = False
    status: str = "verified"
    source_status: str = ""
    source_note: str = ""
    source_ref: dict[str, Any] = field(default_factory=dict)
    display_eligible: bool | None = None
    score_eligible: bool | None = None


@dataclass
class MetricResult(ContractMixin):
    metric_id: str
    name: str
    formula: str
    inputs: list[dict[str, Any]] = field(default_factory=list)
    period: str = ""
    scope: str = ""
    unit: str = ""
    value: Any = None
    display_value: str = ""
    # 代入过程：把输入值及单位带进公式后的算式（含结果），供人工复算；
    # 由计算层确定性生成，模型不得改写，缺失输入时如实标注而不以中性值补齐。
    substitution: str = ""
    threshold: Any = None
    threshold_source: str = ""
    status: str = "calculated"
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    source_status: str = ""
    source_note: str = ""
    source_ref: dict[str, Any] = field(default_factory=dict)
    period_type: str = ""
    comparability: str = ""
    display_eligible: bool | None = None
    score_eligible: bool | None = None


@dataclass
class RiskFinding(ContractMixin):
    risk_id: str
    dimension: str
    title: str
    level: str = "待定级"
    confidence: Any = None
    status: str = "candidate"
    level_status: str = "provisional"
    suggested_level: str = ""
    level_note: str = "建议关注等级（暂定），待补证及人工复核；不构成审计结论。"
    evidence_ids: list[str] = field(default_factory=list)
    metric_ids: list[str] = field(default_factory=list)
    source: str = ""
    reason: str = ""
    involved_accounts: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)
    audit_procedures: list[str] = field(default_factory=list)
    required_materials: list[str] = field(default_factory=list)
    improvement_suggestions: list[str] = field(default_factory=list)
    reinforcement_source: str = ""
    source_status: str = ""
    source_note: str = ""
    source_ref: dict[str, Any] = field(default_factory=dict)
    period_type: str = ""
    comparability: str = ""
    display_eligible: bool | None = None
    score_eligible: bool | None = None


@dataclass
class ArtifactManifest(ContractMixin):
    artifact_id: str
    kind: str
    status: str
    path: str = ""
    url: str = ""
    name: str = ""
    mime_type: str = ""
    exists: bool = False
    accessible: bool = False
    source: str = ""
    analysis_id: str = ""
    data_version: str = DATA_VERSION
    rule_version: str = RULE_VERSION
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str = ""
    snapshot_id: str = ""
    content_hash: str = ""
    data_status: str = SOURCE_STATUS_VERIFIED
    display_only: bool = False
    warning: str = ""


def make_fact(field_name: str, value: Any, *, fact_id: str, unit: str = "",
              currency: str = "", period: str = "", point_in_time: str = "",
              scope: str = "", source_document: str = "", source_hash: str = "",
              page: str = "", locator: str = "", excerpt: str = "",
              extraction_method: str = "") -> Fact:
    """Create a fact while preserving the original representation."""
    raw = "" if value is None else str(value)
    return Fact(
        fact_id=fact_id,
        field=field_name,
        raw_value=raw,
        value=value,
        unit=unit,
        currency=currency,
        period=period,
        point_in_time=point_in_time,
        scope=scope,
        source_document=source_document,
        source_hash=source_hash,
        page=page,
        locator=locator,
        excerpt=excerpt,
        extraction_method=extraction_method,
    )
