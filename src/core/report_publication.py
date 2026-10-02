"""Final report publication helpers.

The final snapshot is the structured source of truth.  This module contains
the small, dependency-light operations shared by the Agent and HTTP layers so
publication code does not need to reconstruct facts from rendered text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from typing import Any

from core.result_contract import ArtifactManifest, DATA_VERSION, RULE_VERSION, derive_task_status


def _json_artifact_safe(value: Any) -> Any:
    """Convert non-finite numeric values so the persisted artifact is valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_artifact_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_artifact_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_artifact_safe(item) for item in value]
    return value


def write_txt_artifact(payload: dict, report: dict) -> str:
    """Persist the JSON ledger as a downloadable plain-text artifact.

    The payload remains valid, pretty-printed JSON for machine readability, but
    the delivery format is TXT so browsers and the frontend never try to lay
    out the full ledger as an inline JSON document.
    """
    if not isinstance(payload, dict) or not isinstance(report, dict):
        return ""
    temp_path = ""
    try:
        from local_storage import upload_file_to_storage
        from utils.filename import build_file_prefix

        prefix = build_file_prefix(report)
        fd, temp_path = tempfile.mkstemp(prefix="audit_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_json_artifact_safe(payload), stream, ensure_ascii=False,
                      indent=2, allow_nan=False, default=str)
            stream.write("\n")
        url = upload_file_to_storage(
            local_path=temp_path,
            file_name=f"reports/{prefix}_JSON风险台账.txt",
            content_type="text/plain; charset=utf-8",
        )
        return url if isinstance(url, str) and url.startswith(("/", "http", "file://")) else ""
    except Exception:
        return ""
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def write_json_artifact(payload: dict, report: dict) -> str:
    """Backward-compatible alias for the TXT-delivered JSON ledger."""
    return write_txt_artifact(payload, report)


def ensure_analysis_identity(report: dict, run_id: str = "") -> str:
    """Ensure a report and its embedded snapshot share one non-empty batch ID."""
    if not isinstance(report, dict):
        return ""
    company = report.get("company_info")
    if not isinstance(company, dict):
        company = {}
        report["company_info"] = company
    analysis_id = str(
        report.get("analysis_id") or company.get("run_id") or run_id or ""
    ).strip()
    if not analysis_id:
        analysis_id = uuid.uuid4().hex
    report["analysis_id"] = analysis_id
    company["run_id"] = analysis_id

    snapshot = report.get("report_snapshot")
    if isinstance(snapshot, dict):
        snapshot["analysis_id"] = analysis_id
        snapshot_company = snapshot.get("company")
        if isinstance(snapshot_company, dict):
            snapshot_company["run_id"] = analysis_id
    return analysis_id


def ensure_snapshot_identity(snapshot: dict, run_id: str = "") -> dict:
    """Return a copied snapshot with a valid analysis ID and stable snapshot ID.

    Existing non-empty snapshot IDs are preserved.  A missing ID is generated
    from the normalized snapshot so callers can safely publish legacy or
    directly-constructed snapshots without emitting an untraceable artifact.
    """
    value = copy.deepcopy(snapshot) if isinstance(snapshot, dict) else {}
    company = value.get("company")
    if not isinstance(company, dict):
        company = {}
        value["company"] = company
    analysis_id = str(value.get("analysis_id") or company.get("run_id") or run_id or "").strip()
    if not analysis_id:
        analysis_id = uuid.uuid4().hex
    value["analysis_id"] = analysis_id
    company["run_id"] = analysis_id
    if not str(value.get("snapshot_id") or "").strip():
        digest_value = copy.deepcopy(value)
        digest_value.pop("snapshot_id", None)
        digest_value.pop("artifact_manifest", None)
        raw = json.dumps(digest_value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
        value["snapshot_id"] = "snap-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return value


def finalize_snapshot_publication(snapshot: dict, artifact_manifest: list[dict],
                                  run_id: str = "") -> dict:
    """Attach the final manifest to a snapshot without changing its snapshot ID."""
    value = ensure_snapshot_identity(snapshot, run_id=run_id)
    value["artifact_manifest"] = copy.deepcopy(artifact_manifest or [])
    return value


def _find_structured_ledger_span(text: str) -> tuple[int, int] | None:
    """Find the first JSON object containing the report ledger fields.

    This is used only to rewrite display text after the structured snapshot is
    already available.  It is deliberately not used to construct the report.
    """
    raw = str(text or "")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            value, end = decoder.raw_decode(raw[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "company_info" in value and "risk_details" in value:
            return match.start(), match.start() + end
    return None


def sync_ai_text_with_snapshot(ai_text: str, snapshot_payload: dict) -> str:
    """Replace the displayed structured ledger with the final snapshot payload.

    Natural-language content is retained.  The structured JSON is treated as a
    compatibility block for old clients, while new clients consume the
    top-level ``report_snapshot`` directly.
    """
    if not isinstance(snapshot_payload, dict):
        return str(ai_text or "")
    span = _find_structured_ledger_span(str(ai_text or ""))
    if not span:
        return str(ai_text or "")
    start, end = span
    return str(ai_text or "")[:start] + json.dumps(
        snapshot_payload, ensure_ascii=False, indent=2
    ) + str(ai_text or "")[end:]


def manifest_hash(path: str) -> str:
    """Return a SHA-256 for an accessible local file, or an empty string."""
    raw = str(path or "")
    if not raw or not os.path.isfile(raw):
        return ""
    digest = hashlib.sha256()
    try:
        with open(raw, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _local_path(path: str, storage_root: str = "") -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    if raw.startswith("/local_storage/"):
        root = storage_root or os.getcwd()
        return os.path.abspath(os.path.join(root, "local_storage", raw[len("/local_storage/"):].replace("/", os.sep)))
    return raw if os.path.isabs(raw) else ""


def finalize_artifact_manifest(expectations: list[dict], candidates: list[dict],
                               analysis_id: str, snapshot_id: str,
                               storage_root: str = "", accessible_fn=None,
                               data_status: str = "verified", warning: str = "") -> list[dict]:
    """Build a typed, deterministic manifest from expected and found artifacts."""
    found = [item for item in (candidates or []) if isinstance(item, dict)]
    used: set[str] = set()
    output: list[dict] = []

    def key_for(path: str) -> str:
        name = os.path.basename(str(path or "")).lower()
        if name.endswith(".xlsx"):
            return "excel"
        if name.endswith(".png") or name.endswith(".jpg") or name.endswith(".jpeg"):
            if "热力" in name:
                return "heatmap"
            if "雷达" in name:
                return "radar"
            if "趋势" in name:
                return "trend"
            return "chart_unknown"
        if name.endswith(".pdf"):
            if "财务健康" in name:
                return "pdf_financial"
            if "合规" in name or "信息披露" in name:
                return "pdf_compliance"
            return "pdf_synthesis"
        if name.endswith(".json"):
            return "json"
        if name.endswith(".txt") and ("json" in name or "风险台账" in name):
            return "json"
        return "file"

    def record(item: dict | None, expectation: dict, ok: bool, error: str = "") -> dict:
        path = str((item or {}).get("path", "") or "")
        local = _local_path(path, storage_root)
        accessible = bool(ok and (
            accessible_fn(path) if accessible_fn is not None
            else local and os.path.isfile(local) and os.access(local, os.R_OK)
        ))
        name = os.path.basename(path) if path else str(expectation.get("label", ""))
        suffix = os.path.splitext(name)[1].lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".pdf": "application/pdf",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".json": "application/json",
                ".txt": "text/plain; charset=utf-8"}.get(suffix, "")
        return ArtifactManifest(
            artifact_id=f"artifact-{len(output) + 1:03d}",
            kind=str(expectation.get("kind", "file")),
            status="success" if accessible else "failed",
            path=path if accessible else "", url=path if accessible else "",
            name=name, mime_type=mime, exists=accessible, accessible=accessible,
            source=str((item or {}).get("tool", "")), analysis_id=analysis_id,
            data_version=DATA_VERSION, rule_version=RULE_VERSION,
            error="" if accessible else (error or "产物不存在或不可访问"),
            snapshot_id=snapshot_id, content_hash=manifest_hash(local) if accessible else "",
            data_status=data_status, display_only=data_status != "verified",
            warning=warning,
        ).to_dict()

    for expectation in expectations or []:
        if not isinstance(expectation, dict):
            continue
        match = next((item for item in found
                      if item.get("path") not in used
                      and key_for(item.get("path", "")) == str(expectation.get("key", ""))
                      and (accessible_fn(item.get("path", "")) if accessible_fn is not None
                           else os.path.isfile(_local_path(item.get("path", ""), storage_root)))), None)
        if match:
            used.add(match.get("path"))
            output.append(record(match, expectation, True))
        else:
            output.append(record(None, expectation, False, "未发现实际生成且可访问的产物链接"))
    for item in found:
        if item.get("path") in used:
            continue
        output.append(record(item, {"kind": key_for(item.get("path", "")),
                                    "label": os.path.basename(str(item.get("path", "")))}, True))
    return output


def publication_metadata(snapshot: dict, manifest: list[dict]) -> dict:
    """Derive public metadata exclusively from the final snapshot and manifest."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    company = snap.get("company") if isinstance(snap.get("company"), dict) else {}
    source = snap.get("source") if isinstance(snap.get("source"), dict) else {}
    validation = snap.get("validation") if isinstance(snap.get("validation"), dict) else {}
    gate = snap.get("review_gate") if isinstance(snap.get("review_gate"), dict) else {}
    return {
        "analysis_id": str(snap.get("analysis_id", "") or ""),
        "snapshot_id": str(snap.get("snapshot_id", "") or ""),
        "data_version": str(snap.get("data_version", DATA_VERSION) or DATA_VERSION),
        "rule_version": str(snap.get("rule_version", RULE_VERSION) or RULE_VERSION),
        "result_schema_version": str(snap.get("schema_version", "") or ""),
        "source_hash": str(source.get("source_hash", "") or ""),
        "validation_status": str(validation.get("validation_result", "") or ""),
        "company_name": str(company.get("company_name", "") or ""),
        "stock_code": str(company.get("stock_code", "") or ""),
        "report_year": str(company.get("report_year", "") or ""),
        "report_period": str(company.get("report_period") or company.get("period") or ""),
        "industry": str(company.get("industry", "") or ""),
        "audit_opinion": str(company.get("audit_opinion", "") or ""),
        "accounting_standard": str(company.get("accounting_standard", "") or ""),
        "review_gate_status": str(gate.get("status", "not_run") or "not_run"),
        "human_review_required": bool(gate.get("human_review_required")),
        "artifact_status": derive_task_status(manifest),
        "artifact_success_count": sum(1 for item in manifest if item.get("status") == "success"),
        "artifact_total": len(manifest),
    }
