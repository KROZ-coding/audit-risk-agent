# -*- coding: utf-8 -*-
"""安全加固回归测试（对应 security-scan 发现的 6 项）

覆盖：
- SSRF 防护：_assert_public_host 拒绝内网/回环/元数据地址
- 路径遍历防护：_resolve_file_path 拒绝逃逸工作目录的相对/绝对路径
- run_id 强格式校验：new_context 拒绝可推测/畸形 x-run-id
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestSSRFGuard:
    """pdf_parser._assert_public_host：拒绝非公网地址（CWE-918）"""

    def test_rejects_loopback(self):
        from tools.pdf_parser import _assert_public_host
        with pytest.raises(ValueError):
            _assert_public_host("http://127.0.0.1/x.pdf")

    def test_rejects_metadata_endpoint(self):
        from tools.pdf_parser import _assert_public_host
        with pytest.raises(ValueError):
            _assert_public_host("http://169.254.169.254/latest/meta-data/")

    def test_rejects_private_range(self):
        from tools.pdf_parser import _assert_public_host
        with pytest.raises(ValueError):
            _assert_public_host("http://192.168.1.1/x.pdf")

    def test_rejects_empty_host(self):
        from tools.pdf_parser import _assert_public_host
        with pytest.raises(ValueError):
            _assert_public_host("http:///x.pdf")


class TestPathTraversalGuard:
    """pdf_parser._resolve_file_path：拒绝逃逸工作目录（CWE-22）"""

    def test_rejects_relative_escape(self, monkeypatch, tmp_path):
        from tools import pdf_parser
        monkeypatch.setenv("COZE_WORKSPACE_PATH", str(tmp_path))
        with pytest.raises(ValueError):
            pdf_parser._resolve_file_path("../../../etc/passwd")

    def test_rejects_absolute_outside(self, monkeypatch, tmp_path):
        from tools import pdf_parser
        monkeypatch.setenv("COZE_WORKSPACE_PATH", str(tmp_path))
        outside = str(tmp_path.parent / "secret.pdf")
        with pytest.raises(ValueError):
            pdf_parser._resolve_file_path(outside)

    def test_allows_inside_workspace(self, monkeypatch, tmp_path):
        from tools import pdf_parser
        monkeypatch.setenv("COZE_WORKSPACE_PATH", str(tmp_path))
        (tmp_path / "reports").mkdir()
        resolved = pdf_parser._resolve_file_path("reports/a.pdf")
        assert resolved.startswith(os.path.realpath(str(tmp_path)))


class TestRunIdValidation:
    """local_shims.new_context：x-run-id 强格式校验（CWE-384）"""

    def test_accepts_valid_uuid_hex(self):
        from local_shims import new_context
        valid = "a" * 32
        ctx = new_context(headers={"x-run-id": valid})
        assert ctx.run_id == valid

    def test_rejects_short_predictable(self):
        from local_shims import new_context
        ctx = new_context(headers={"x-run-id": "admin"})
        assert ctx.run_id != "admin" and len(ctx.run_id) == 32

    def test_rejects_injection_chars(self):
        from local_shims import new_context
        ctx = new_context(headers={"x-run-id": "../../etc; drop"})
        assert ctx.run_id != "../../etc; drop"

    def test_empty_generates_new(self):
        from local_shims import new_context
        ctx = new_context(headers={})
        assert len(ctx.run_id) == 32

    def test_normalize_run_id_reusable(self):
        """normalize_run_id 供 HTTP 入口复用：入口覆盖 run_id 时不得绕过强校验。

        回归：/run 与 /stream_run 曾用 request.headers 直接覆盖 ctx.run_id，
        绕过了 new_context 的正则校验（会话固定防护失效）。
        """
        from local_shims import normalize_run_id
        assert normalize_run_id("admin") != "admin"          # 可推测短串→重生
        assert normalize_run_id("../../etc") != "../../etc"  # 注入串→重生
        assert len(normalize_run_id("x")) == 32
        valid = "a" * 32
        assert normalize_run_id(valid) == valid              # 强格式→原样保留
