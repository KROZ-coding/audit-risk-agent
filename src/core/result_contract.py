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
        return _json_value(asdict(self))


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


@dataclass
class RiskFinding(ContractMixin):
    risk_id: str
    dimension: str
    title: str
    level: str = "待定级"
    confidence: Any = None
    status: str = "candidate"
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
