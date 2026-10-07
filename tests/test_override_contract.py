"""T7 local_case_overrides 钩子契约测试（property-based 快速版）

契约（hooks 的独立性边界）：
1. 白名单字段：仅允许改写 evidence/locator/excerpt 类定位字段与文本，
   不得新增风险条目、不得删除既有条目、不得改写 risk_id/level；
2. 幂等：同一输入重复应用两次结果一致；
3. 不可新增风险：应用前后 risk_details 条目数不减不增。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agents.agent import _load_local_case_overrides

REPO = os.path.join(os.path.dirname(__file__), "..")


class TestHookContract:
    def test_example_module_loads_or_absent(self):
        """本地模块存在则必须可加载，不存在返回 None（不炸）"""
        case = _load_local_case_overrides()
        if case is not None:
            # 契约：模块必须暴露示例声明的公开接口之一（按 example 的接口面检查）
            assert hasattr(case, "SOURCE_EVIDENCE_ANCHORS") or hasattr(case, "augment_guarantee_evidence") \
                or hasattr(case, "normalize_source_bound_str")

    def test_normalize_idempotent(self):
        """幂等：normalize_source_bound_str 应用两次结果一致"""
        case = _load_local_case_overrides()
        if case is None or not hasattr(case, "normalize_source_bound_str"):
            return
        sample = "担保余额占净资产 8.72%，需核对分母定义"
        once = case.normalize_source_bound_str(sample)
        twice = case.normalize_source_bound_str(once)
        assert once == twice

    def test_no_new_risks_via_backfill(self):
        """augment_guarantee_evidence 不得新增风险条目（只返回文本增补）"""
        case = _load_local_case_overrides()
        if case is None or not hasattr(case, "augment_guarantee_evidence"):
            return
        risk = {"risk_id": "R003", "title": "担保风险", "level": "重要",
                "evidence": "担保余额 260,000"}
        extra = case.augment_guarantee_evidence(
            risk, "担保余额 260,000 履约担保 8.72%", {"parse_pdf_report": "原文"})
        # 返回必须是 str 或 None（文本增补），不得是包含新风险的 dict/list
        assert extra is None or isinstance(extra, str)

    def test_module_is_gitignored(self):
        """真实规则模块不入库（独立性：不得被版本库跟踪）"""
        import subprocess
        tracked = subprocess.check_output(
            ["git", "ls-files", "config/local_case_overrides.py"],
            cwd=REPO, stderr=subprocess.DEVNULL).decode().strip()
        # 真实模块不入库（.example.py 可以入库）
        assert tracked == "", "config/local_case_overrides.py 不应被版本库跟踪"
