"""单一离线验收入口（方案 §9.3）：机器可读、fail-closed。

用法（推荐离线模式，不调远程模型、不读生产 .env）：

    python scripts/check_audit_release.py --offline --case petrochina_2025_h1
    python scripts/check_audit_release.py --offline          # 默认石油案例（必须显式 --offline）
    python scripts/check_audit_release.py --offline --pytest tests/   # 也可显式指定目录

约定（§9.3）：以下任一项出现 → 判定不通过，退出非零：
- 被测命令超时 / 非零退出码 / pytest 收集数为零 / 必要测试被跳过 /
  断言数量为零 / 输出为空 / 必需 fixture（源 PDF）缺失；
- 任何环境开关试图触发真实服务调用（远程模型 / 读取生产 .env / 联网评分）。

本脚本离线模式在导入时即屏蔽真实服务边界：不加载 .env、不构造 LLM Agent、
不调用任何远程模型。所有指标来自本机确定性工具链与本地 PDF fixture；
本地存在真实年报时优先使用真实年报，否则使用仓库内的脱敏 CI fixture。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ── 离线守卫：一旦有人试图读取生产凭据或构造远程模型即抛出 ──
_BLOCKED_ENV_KEYS = ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY", "MODEL_API_KEY")
_REAL_SERVICE_BLOCKED = {"_blocked": False}


def _guard_env():
    """离线模式禁用任何生产密钥；发现已注入的密钥即标记并拒绝继续。"""
    leaked = [k for k in _BLOCKED_ENV_KEYS if os.environ.get(k)]
    if leaked:
        _REAL_SERVICE_BLOCKED["_blocked"] = True
        return leaked
    return []


def _block_remote_model_imports():
    """把远程/LLM 真正入口占位为抛错函数，防止误触发真实调用。"""
    import builtins
    orig_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "agents" or name.startswith("agents."):
            raise RuntimeError(
                "check_audit_release 离线模式禁止加载 agents（涉及真实 LLM 调用边界）"
            )
        return orig_import(name, *args, **kwargs)

    builtins.__import__ = guarded


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(repo: str) -> dict:
    """返回当前 commit 与工作区脏文件数（不联网、不 commit/push）。"""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo,
            capture_output=True, text=True, timeout=10,
        ).stdout.splitlines()
        return {"head": head, "dirty": bool(dirty), "dirty_file_count": len(dirty)}
    except Exception:  # noqa: BLE001 - 非 git 环境也要能输出占位
        return {"head": "", "dirty": True, "dirty_file_count": -1}


def run_pytest(targets: list[str], timeout_seconds: int) -> dict:
    """子进程运行 pytest，超时/非零/零收集均按失败处理并返回结构化统计。"""
    cmd = [sys.executable, "-X", "utf8", "-m", "pytest"]
    if not sys.flags.utf8_mode:
        cmd = [sys.executable, "-m", "pytest"]
    cmd += ["-p", "no:cacheprovider", "-q", "--tb=short"]
    cmd += list(targets)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": ["timeout"], "returncode": -1,
                "collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0,
                "output": "[timeout]"}

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    collected = _parse_count(out, r"collected (\d+)")
    passed = _parse_count(out, r"(\d+) passed")
    failed = _parse_count(out, r"(\d+) failed")
    skipped = _parse_count(out, r"(\d+) skipped")
    errors = _parse_count(out, r"(\d+) error")
    deselected = _parse_count(out, r"(\d+) deselected")
    xfailed = _parse_count(out, r"(\d+) xfailed")
    xpassed = _parse_count(out, r"(\d+) xpassed")
    if collected == 0:
        # pytest -q 不会打印 "collected N items"：由汇总计数反推
        collected = passed + failed + skipped + errors + deselected + xfailed + xpassed
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "collected": collected, "passed": passed,
        "failed": failed, "skipped": skipped, "errors": errors,
        "why": [],
        "output": out[-6000:],
    }


def _parse_count(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


# ── 离线工具链核查：仅本地确定性工具，与 offline_real_verify 同源 ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.data_validator import validate_financial_data
from tools.financial_calculator import calculate_financial_indicators
from tools.disclosure_checker import check_disclosure_compliance
from tools.audit_opinion import identify_audit_opinion
from tools.multi_year_comparison import compare_multi_year
from offline_real_verify import CALC_JSON, VD_JSON, MY_INPUT, build_ledger


def run_offline_toolchain(pdf_path: str, text: str) -> dict:
    """跑真实确定性工具链（无 LLM），返回供门禁判定的结构化结果。"""
    vd_raw = validate_financial_data.invoke({"financial_data_json": json.dumps(VD_JSON)})
    fin_raw = calculate_financial_indicators.invoke({"financial_data_json": json.dumps(CALC_JSON)})
    dc_raw = check_disclosure_compliance.invoke({"report_text": text[:200000]})
    ao_raw = identify_audit_opinion.invoke({"report_text": text[:200000]})
    my_raw = compare_multi_year.invoke({"multi_year_data_json": json.dumps(MY_INPUT)})

    fin = json.loads(fin_raw)
    dc = json.loads(dc_raw)
    vd = json.loads(vd_raw)
    ao = json.loads(ao_raw)
    try:
        my = json.loads(my_raw) if isinstance(my_raw, str) and my_raw.startswith("{") else {}
    except Exception:  # noqa: BLE001
        my = {}

    ledger = build_ledger(fin, dc, vd, ao, my)
    facts = fin.get("facts") or []
    validation = vd.get("data_validation") or {}
    metrics = fin.get("metric_results") or []
    return {
        "toolchain": {
            "alerts": len(fin.get("alerts", [])),
            "disclosure_issues": len(dc.get("issues", [])),
            "validation_result": validation.get("validation_result", ""),
            "audit_opinion": str((ao.get("audit_opinion") or {}).get("opinion_type", "")),
            "trend_alerts": my.get("alert_count", 0),
        },
        "ledger": {
            "risk_items": len(ledger.get("risk_details", [])),
            "major": ledger.get("risk_summary", {}).get("major_risks", 0),
            "important": ledger.get("risk_summary", {}).get("important_risks", 0),
            "general": ledger.get("risk_summary", {}).get("general_risks", 0),
        },
        "facts": {
            "valid_fact_count": len(facts),
            "metric_count": len(metrics),
            "compared_field_count": len(metrics) + len(facts),
        },
        "gross_margin_pct": round(next(
            (m["value"] for m in metrics if m["metric_id"] == "gross_margin_pct"), None), 2),
        "validation_failed": validation.get("failed_checks", 0),
    }


def _extract_pdf_text(pdf_path: str) -> str:
    """本地解析源 PDF 前 200k 字符（不联网、不调模型）。"""
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    return "\n\n".join(
        f"--- 第 {i + 1} 页 ---\n{pg.extract_text() or ''}"
        for i, pg in enumerate(reader.pages)
    )[:200_000]


def _check_fixture(pdf_path: str) -> bool:
    return bool(pdf_path) and os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0


def _default_pdf_path(repo: Path) -> str:
    """Prefer the local real report, then fall back to the sanitized CI fixture."""
    candidates = [
        repo / "测试" / "中国石油：中国石油天然气股份有限公司2025 年半年度报告.pdf",
        repo / "tests" / "fixtures" / "petrochina_2025_h1_sanitized.pdf",
    ]
    for candidate in candidates:
        if _check_fixture(str(candidate)):
            return str(candidate)
    return str(candidates[0])


def _snapshot_id_of(validation: dict, git: dict) -> str:
    """派生统一快照 ID（与 _apply_review_gates 同规则的可复现版本）。"""
    meta = {
        "analysis_id": "offline_release_gate",
        "git_head": git.get("head", ""),
        "validation_status": (validation.get("data_validation") or {}).get("validation_result", ""),
    }
    raw = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    return "snap-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def check(case: str = "petrochina_2025_h1", run_pytest_full: bool = True,
          pytest_targets: list[str] | None = None, timeout_seconds: int = 300,
          pdf_path: str | None = None) -> dict:
    """执行离线验收，返回机器可读字典。fail-closed：任何异常条件都置 ok=False。"""
    problems: list[str] = []

    # 1) 离线守卫：生产密钥一旦存在即失败（防止误触真实服务）
    leaked = _guard_env()
    if leaked:
        problems.append(f"检测到生产凭据泄露进入本进程：{leaked}")

    # 2) PDF fixture 必须存在（§9.3 必需 fixture 缺失不得通过）
    if pdf_path is None:
        repo = Path(__file__).resolve().parent.parent
        pdf_path = _default_pdf_path(repo)
    if not _check_fixture(pdf_path):
        problems.append(f"必需 fixture（源 PDF）缺失或为空: {pdf_path}")

    git = git_head(Path(__file__).resolve().parent.parent)

    # 3) 真实确定性工具链（本地，无 LLM）
    tool = {}
    if _check_fixture(pdf_path):
        text = _extract_pdf_text(pdf_path)
        tool = run_offline_toolchain(pdf_path, text)
        if tool["gross_margin_pct"] is None:
            problems.append("离线工具链未产出毛利率（指标计算失败/输出为空）")
        if tool["toolchain"]["validation_result"] not in ("通过", "部分完成"):
            problems.append("数据校验失败：" + str(tool["toolchain"]["validation_result"]))
        if tool["toolchain"]["alerts"] is None or tool["facts"]["valid_fact_count"] == 0:
            problems.append("离线工具链输出为空（无有效事实）")

    # 4) 聚焦回归 pytest（§9.3：必要测试跳过 / 零收集 / 断言为零均失败）
    if pytest_targets is None:
        pytest_targets = [
            str(Path(__file__).resolve().parent.parent / "tests" / "test_offline_real_verify.py"),
            str(Path(__file__).resolve().parent.parent / "tests" / "test_fact_contract.py"),
            str(Path(__file__).resolve().parent.parent / "tests" / "test_financial_rules.py"),
            str(Path(__file__).resolve().parent.parent / "tests" / "test_report_publication.py"),
            str(Path(__file__).resolve().parent.parent / "tests" / "test_report_consistency.py"),
        ]
    pt = run_pytest(pytest_targets, timeout_seconds)
    if not pt["ok"]:
        problems.append("pytest 非零退出或超时")
    if pt["collected"] == 0:
        problems.append("pytest 测试收集数为零")
    if pt["failed"] or pt["errors"]:
        problems.append(f"pytest 失败 {pt['failed']} / 错误 {pt['errors']}")
    if pt["skipped"] > 0:
        problems.append(f"存在被跳过的必要测试 {pt['skipped']} 项")
    if pt["passed"] < 1:
        problems.append("断言数量为零（没有任何测试通过）")
    if not pt["output"].strip():
        problems.append("pytest 输出为空")

    snapshot_id = _snapshot_id_of({"data_validation": {"validation_result": tool.get("toolchain", {}).get("validation_result", "")}}, git)

    return {
        "ok": not problems,
        "mode": "offline",
        "case": case,
        "code_version": {
            "git_head": git["head"],
            "workspace_dirty": git["dirty"],
            "dirty_file_count": git["dirty_file_count"],
        },
        "source_pdf": {
            "exists": _check_fixture(pdf_path),
            "sha256": sha256_of(pdf_path) if _check_fixture(pdf_path) else "",
        },
        "snapshot_id": snapshot_id,
        "remote_model_called": False,
        "toolchain": tool.get("toolchain", {}),
        "ledger": tool.get("ledger", {}),
        "facts": tool.get("facts", {}),
        "gross_margin_pct": tool.get("gross_margin_pct"),
        "pytest": pt,
        "fail_closed": {"problems": problems},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计离线验收入口（fail-closed）")
    parser.add_argument("--offline", action="store_true", default=False,
                        help="离线模式（必须显式传入）：不调远程模型、不读生产 .env")
    parser.add_argument("--case", default="petrochina_2025_h1")
    parser.add_argument("--pytest", nargs="*", default=None, help="指定 pytest 目标（默认聚焦回归集）")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--pdf", default=None, help="显式指定源 PDF 路径")
    args = parser.parse_args(argv)

    if not args.offline:
        result = {"ok": False, "fail_closed": {"problems": [
            "本脚本仅支持离线模式；未提供阻止真实服务调用的在线实现。"]},
            "mode": "unknown", "case": args.case}
        _emit(result)
        return 1

    _block_remote_model_imports()
    leaked = _guard_env()
    if leaked:
        result = {"ok": False, "mode": "offline", "case": args.case,
                  "fail_closed": {"problems": [f"生产凭据泄露：{leaked}"]}}
        _emit(result)
        return 1

    result = check(case=args.case, pytest_targets=args.pytest,
                   timeout_seconds=args.timeout, pdf_path=args.pdf)
    _emit(result)
    return 0 if result["ok"] else 1


def _emit(result: dict) -> None:
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
