"""单一离线验收入口（§9.3）的 fail-closed 行为测试。

直接驱动 scripts/check_audit_release.py 的真实逻辑；对需要外部资源的边界
（源 PDF 工具链、pytest 子进程）打桩，验证"任何异常条件都不得显示通过"：
- 生产密钥泄露 -> 失败
- 必需 fixture（源 PDF）缺失/为空 -> 失败
- pytest 非零退出 / 零收集 / 跳过 / 断言为零 / 输出为空 -> 失败
- 显式请求在线模式 -> 拒绝且失败
- 正常离线路径 -> 通过，且 remote_model_called=False、输出机器可读
"""

import importlib
import io
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import check_audit_release as car


@pytest.fixture(autouse=True)
def _reset_guard():
    import builtins
    orig_import = builtins.__import__
    car._REAL_SERVICE_BLOCKED["_blocked"] = False
    for key in car._BLOCKED_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    builtins.__import__ = orig_import  # main() 会安装 import 守卫，测试后必须恢复
    car._REAL_SERVICE_BLOCKED["_blocked"] = False
    for key in car._BLOCKED_ENV_KEYS:
        os.environ.pop(key, None)


def _stub_pytest(monkeypatch, *, returncode=0, text="51 passed, 0 failed, 0 skipped",
                collected=None, passed=None, skipped=0, failed=0, errors=0):
    if collected is None:
        collected = (passed if passed is not None else 51) + skipped + failed + errors
    if passed is None:
        passed = 51
    monkeypatch.setattr(car, "run_pytest", lambda *a, **k: {
        "ok": returncode == 0, "returncode": returncode, "collected": collected,
        "passed": passed, "failed": failed, "skipped": skipped, "errors": errors,
        "why": [], "output": text,
    })


def _stub_pdf(monkeypatch, tmp_path):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(car, "_check_fixture", lambda p: os.path.isfile(p) and os.path.getsize(p) > 0)
    monkeypatch.setattr(car, "sha256_of", lambda p: "deadbeef" * 4)
    monkeypatch.setattr(car, "_extract_pdf_text", lambda p: "fake pdf text")
    # 工具链打桩：返回一个结构完整、可判通过的确定性结果
    monkeypatch.setattr(car, "run_offline_toolchain", lambda pdf, text: {
        "toolchain": {"alerts": 0, "disclosure_issues": 2,
                      "validation_result": "部分完成", "audit_opinion": "未经审计（半年度报告）",
                      "trend_alerts": 0},
        "ledger": {"risk_items": 3, "major": 0, "important": 3, "general": 0},
        "facts": {"valid_fact_count": 34, "metric_count": 21, "compared_field_count": 55},
        "gross_margin_pct": 20.89,
        "validation_failed": 0,
    })
    return str(pdf)


def test_offline_ok_when_all_guards_pass(monkeypatch, tmp_path):
    pdf = _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch)
    result = car.check(pdf_path=pdf, pytest_targets=["tests"])
    assert result["ok"] is True
    assert result["mode"] == "offline"
    assert result["remote_model_called"] is False
    assert result["gross_margin_pct"] == 20.89
    assert result["toolchain"]["validation_result"] == "部分完成"
    assert result["source_pdf"]["sha256"] == "deadbeef" * 4
    assert result["snapshot_id"].startswith("snap-")
    assert "git_head" in result["code_version"]
    assert result["fail_closed"]["problems"] == []
    assert result["pytest"]["passed"] == 51


def test_production_secret_leak_fails(monkeypatch, tmp_path):
    pdf = _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch)
    os.environ["OPENAI_API_KEY"] = "sk-test-leak"
    result = car.check(pdf_path=pdf, pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("凭据" in p for p in result["fail_closed"]["problems"])


def test_missing_pdf_fixture_fails(monkeypatch):
    _stub_pytest(monkeypatch)
    monkeypatch.setattr(car, "_check_fixture", lambda p: False)
    result = car.check(pdf_path="C:/no/such/file.pdf", pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("fixture" in p or "源 PDF" in p for p in result["fail_closed"]["problems"])
    assert result["source_pdf"]["exists"] is False


def test_empty_pdf_fixture_fails(monkeypatch, tmp_path):
    pdf = tmp_path / "empty.pdf"
    pdf.write_bytes(b"")
    _stub_pytest(monkeypatch)
    monkeypatch.setattr(car, "_check_fixture", lambda p: False)
    result = car.check(pdf_path=str(pdf), pytest_targets=["tests"])
    assert result["ok"] is False


def test_pytest_nonzero_exit_fails(monkeypatch, tmp_path):
    _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch, returncode=1, text="1 failed, 50 passed")
    result = car.check(pdf_path=tmp_path / "sample.pdf", pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("非零退出" in p for p in result["fail_closed"]["problems"])


def test_pytest_zero_collected_fails(monkeypatch, tmp_path):
    _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch, returncode=5, text="no tests ran", collected=0, passed=0)
    result = car.check(pdf_path=tmp_path / "sample.pdf", pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("收集数为零" in p for p in result["fail_closed"]["problems"])


def test_pytest_skipped_fails(monkeypatch, tmp_path):
    _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch, text="50 passed, 1 skipped, 0 failed", collected=51, passed=50, skipped=1)
    result = car.check(pdf_path=tmp_path / "sample.pdf", pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("跳过" in p for p in result["fail_closed"]["problems"])


def test_pytest_zero_passed_fails(monkeypatch, tmp_path):
    _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch, returncode=1, text="0 passed, 51 failed", collected=51, passed=0, failed=51)
    result = car.check(pdf_path=tmp_path / "sample.pdf", pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("断言数量为零" in p for p in result["fail_closed"]["problems"])


def test_pytest_empty_output_fails(monkeypatch, tmp_path):
    _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch, text="")
    result = car.check(pdf_path=tmp_path / "sample.pdf", pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("输出为空" in p for p in result["fail_closed"]["problems"])


def test_toolchain_missing_gross_margin_fails(monkeypatch, tmp_path):
    pdf = _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch)
    monkeypatch.setattr(car, "run_offline_toolchain", lambda pdf, text: {
        "toolchain": {"alerts": 0, "disclosure_issues": 0,
                      "validation_result": "部分完成", "audit_opinion": "", "trend_alerts": 0},
        "ledger": {"risk_items": 0, "major": 0, "important": 0, "general": 0},
        "facts": {"valid_fact_count": 1, "metric_count": 1, "compared_field_count": 2},
        "gross_margin_pct": None,
        "validation_failed": 0,
    })
    result = car.check(pdf_path=pdf, pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("毛利率" in p for p in result["fail_closed"]["problems"])


def test_toolchain_zero_facts_fails(monkeypatch, tmp_path):
    pdf = _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch)
    monkeypatch.setattr(car, "run_offline_toolchain", lambda pdf, text: {
        "toolchain": {"alerts": 0, "disclosure_issues": 0,
                      "validation_result": "通过", "audit_opinion": "", "trend_alerts": 0},
        "ledger": {"risk_items": 0, "major": 0, "important": 0, "general": 0},
        "facts": {"valid_fact_count": 0, "metric_count": 0, "compared_field_count": 0},
        "gross_margin_pct": 20.89,
        "validation_failed": 0,
    })
    result = car.check(pdf_path=pdf, pytest_targets=["tests"])
    assert result["ok"] is False
    assert any("无有效事实" in p for p in result["fail_closed"]["problems"])


def test_online_mode_is_rejected(monkeypatch, capsys):
    # 不带 --offline 即视为在线模式：直接拒绝且退出非零
    monkeypatch.setattr(car, "_emit", lambda r: None)
    rc = car.main([])
    assert rc == 1


def test_main_default_offline_rejects_leaked_secret(monkeypatch, capsys):
    os.environ["DASHSCOPE_API_KEY"] = "sk-test-leak"
    captured = {}
    monkeypatch.setattr(car, "_emit", lambda r: captured.update(r))
    rc = car.main(["--offline"])
    assert rc == 1
    assert captured["ok"] is False
    assert any("凭据" in p for p in captured["fail_closed"]["problems"])


def test_main_returns_json_to_stdout(monkeypatch, tmp_path, capsys):
    pdf = _stub_pdf(monkeypatch, tmp_path)
    _stub_pytest(monkeypatch)
    rc = car.main(["--offline", "--pdf", pdf])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True
    assert data["remote_model_called"] is False
    assert data["snapshot_id"].startswith("snap-")
    assert rc == 0


def test_block_remote_model_import_guards_agents(monkeypatch):
    import builtins
    orig = builtins.__import__
    try:
        car._block_remote_model_imports()
        with pytest.raises(RuntimeError):
            builtins.__import__("agents", globals(), locals(), [], 0)
        with pytest.raises(RuntimeError):
            builtins.__import__("agents.some_module", globals(), locals(), [], 0)
        # 非 agents 模块不受影响
        builtins.__import__("json", globals(), locals(), [], 0)
    finally:
        builtins.__import__ = orig
