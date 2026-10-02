"""Risk count floors must never consume unreviewed candidate priorities."""

import copy
import json

import pytest
from langchain_core.messages import AIMessage

from agents.agent import (
    _build_risk_index_md,
    _enforce_risk_level_floor,
    _mark_score_review_status,
    _sync_summary_into_message,
)


def _report(status="accepted"):
    return {
        "risk_details": [
            {"risk_id": f"R{i}", "level": "重要", "formal_status": status}
            for i in range(3)
        ],
        "comprehensive_score": {
            "score": 14.5, "level": "低风险", "level_key": "low",
            "base_score": 10.5, "escalation": 4, "escalation_reasons": ["原工具规则"],
        },
        "comprehensive_score_snapshot": {"score": 14.5, "level": "低风险"},
        "overall_assessment": "综合风险评分14.5分（低风险）。",
    }


@pytest.mark.parametrize("candidate_marker", [
    {}, {"formal_status": "unaccepted"},
    {"formal_status": "accepted", "pending_verification": True},
    {"formal_status": "accepted", "evidence_pending": True},
    {"formal_status": "accepted", "level_status": "provisional"},
])
def test_candidates_never_trigger_count_floor(candidate_marker):
    report = _report()
    for risk in report["risk_details"]:
        risk.pop("formal_status")
        risk.update(candidate_marker)
    original_score = copy.deepcopy(report["comprehensive_score"])
    assert _enforce_risk_level_floor(report) is None
    assert report["comprehensive_score"] == original_score
    assert "level_floor_note" not in report


def test_accepted_floor_preserves_underlying_quantitative_score_and_escalation():
    report = _report()
    result = _enforce_risk_level_floor(report)
    score = json.loads(result[0])
    assert score["score"] == 26
    assert score["quantitative_score"] == 14.5
    assert score["base_score"] == 10.5
    assert score["escalation"] == 4
    assert score["level_floor_adjustment"] == 11.5
    assert score["score"] == score["base_score"] + score["escalation"] + score["level_floor_adjustment"]
    assert "已采信风险" in report["level_floor_note"]


def test_reapplying_floor_then_withdrawing_risks_restores_tool_score():
    report = _report()
    _enforce_risk_level_floor(report)
    _enforce_risk_level_floor(report)
    assert report["comprehensive_score"]["level_floor_adjustment"] == 11.5
    for risk in report["risk_details"]:
        risk["formal_status"] = "unaccepted"
    _enforce_risk_level_floor(report)
    assert report["comprehensive_score"]["score"] == 14.5
    assert report["comprehensive_score"]["base_score"] == 10.5
    assert report["comprehensive_score"]["escalation_reasons"] == ["原工具规则"]
    assert report["comprehensive_score_snapshot"] == {"score": 14.5, "level": "低风险"}
    assert "level_floor_note" not in report
    assert "26.0" not in report["overall_assessment"]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True])
def test_unavailable_or_invalid_score_is_not_invented(value):
    report = _report()
    report["comprehensive_score"]["score"] = value
    assert _enforce_risk_level_floor(report) is None


def test_pending_priority_and_score_scope_are_visible():
    report = _report("unaccepted")
    report["pending_items"] = report["risk_details"]
    report["review_gate"] = {"status": "not_passed"}
    _mark_score_review_status(report)
    assert report["comprehensive_score"]["score"] == 14.5
    assert report["comprehensive_score"]["assessment_status"] == "pending_review"
    assert report["comprehensive_score"]["pending_risk_count"] == 3
    assert "不能据此认定整体低风险" in report["score_review_note"]
    index = _build_risk_index_md(report)
    assert "重要（暂定关注）" in index
    assert "待复核" in index


def test_score_records_follow_floor_and_retraction_in_both_export_locations():
    report = _report()
    records = {
        "facts": [{"fact_id": "F-SCORE-TOTAL", "value": 14.5}],
        "metric_results": [{"metric_id": "score_total", "value": 14.5}],
        "evidence": [{"evidence_id": "E-SCORE-TOTAL", "excerpt": "综合评分 14.5"}],
    }
    report.update(copy.deepcopy(records))
    report["comprehensive_score"].update(copy.deepcopy(records))
    for expected in (26, 14.5):
        if expected == 14.5:
            for risk in report["risk_details"]:
                risk["formal_status"] = "unaccepted"
        _enforce_risk_level_floor(report)
        _mark_score_review_status(report)
        for container in (report, report["comprehensive_score"]):
            assert container["facts"][0]["value"] == expected
            metric = container["metric_results"][0]
            assert metric["value"] == expected
            assert sum(item["value"] for item in metric["inputs"]) == expected
            assert f"= {expected:g} 分" in container["evidence"][0]["excerpt"]


def test_overview_is_rebuilt_from_final_status_and_removes_stale_chart():
    report = _report("unaccepted")
    report["risk_details"][0].update(title="应收与营收背离", evidence="应收同比上升", dimension="financial_misstatement")
    message = AIMessage(content=("## 一、概况\n原始背景\n## 二、双模块结论汇总\n"
                                "| 旧结论 | 重大 |\n"
                                '```echarts\n{"title":{"text":"风险评分"},"series":[{"data":[70]}]}\n```\n'
                                "## 三、详细分析\n保留原始论证"))
    assert _sync_summary_into_message(message, report)
    # 只加粗裸等级词，「（暂定关注）」留在粗体之外：前端徽章正则只识别裸等级，
    # 整串加粗会在后缀之后留下游离的 </strong>（实测渲染事故，见前端 F15 用例）。
    assert "**重要**（暂定关注）" in message.content
    assert "**重要（暂定关注）**" not in message.content
    assert "旧结论" not in message.content
    assert "echarts" not in message.content
    assert "保留原始论证" in message.content
    assert "原始背景" in message.content
    # 双模块结论改为「分模块 + 分点」，不再用一张宽表压扁依据摘要
    assert "### 财务健康度诊断" in message.content
    assert "### 合规与经营风险扫描" in message.content
    assert "| 模块 | 关注事项 |" not in message.content
    bullets = [line for line in message.content.splitlines() if line.startswith("- ")]
    assert len(bullets) == len(report["risk_details"]), "分点数必须等于台账条目数"
    assert all("风险等级：" in line and "依据：" in line for line in bullets)


def test_overview_preserves_following_ledger_without_next_heading():
    ledger = json.dumps({"risk_details": []}, ensure_ascii=False)
    message = AIMessage(content="## 双模块结论汇总\n旧表格\n```json\n" + ledger + "\n```\n")
    assert _sync_summary_into_message(message, _report())
    assert ledger in message.content
    assert message.content.count("```json") == 1


def test_heading_inside_fenced_json_is_not_rewritten():
    message = AIMessage(content='```text\n## 双模块结论汇总\n原始材料\n```\n')
    original = message.content
    assert not _sync_summary_into_message(message, _report())
    assert message.content == original
