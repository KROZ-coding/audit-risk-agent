"""JSON 台账作为独立下载产物的回归测试。"""

import json


def test_write_json_artifact_persists_complete_valid_payload(monkeypatch):
    from core.report_publication import write_json_artifact

    captured = {}

    def upload(local_path, file_name, content_type, **_kwargs):
        with open(local_path, encoding="utf-8") as stream:
            captured["payload"] = stream.read()
        captured["file_name"] = file_name
        captured["content_type"] = content_type
        return "/local_storage/20260917_120000/reports/ledger.txt"

    monkeypatch.setattr("local_storage.upload_file_to_storage", upload)
    payload = {
        "company_info": {"company_name": "JSON产物测试公司"},
        "risk_details": [{"risk_id": "R001", "evidence": "完整证据"}],
        "calculated": {"missing_value": float("nan")},
    }

    url = write_json_artifact(payload, payload)

    assert url.endswith("ledger.txt")
    assert captured["content_type"] == "text/plain; charset=utf-8"
    assert captured["file_name"].endswith("_JSON风险台账.txt")
    parsed = json.loads(captured["payload"])
    assert parsed["risk_details"][0]["evidence"] == "完整证据"
    assert parsed["calculated"]["missing_value"] is None


def test_json_artifact_is_a_manifest_kind():
    from core.report_publication import finalize_artifact_manifest

    manifest = finalize_artifact_manifest(
        [{"key": "json", "kind": "json", "label": "TXT格式结构化风险台账（JSON内容）"}],
        [{"tool": "txt_artifact", "path": "/local_storage/reports/JSON风险台账.txt"}],
        "run-json", "snap-json", accessible_fn=lambda _path: True,
    )

    assert manifest[0]["kind"] == "json"
    assert manifest[0]["status"] == "success"
    assert manifest[0]["mime_type"] == "text/plain; charset=utf-8"
