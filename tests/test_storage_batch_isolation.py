# -*- coding: utf-8 -*-
"""产物批次隔离测试（用户需求：每次生成在下载文件夹里加一个精确到秒的时间戳文件夹）。

覆盖目标：
- begin_batch 后同一次分析的所有上传（reports/ + charts/）共享同一时间戳子目录；
- 下一次 begin_batch 生成全新目录，旧批次文件不删除、也不被新批次覆盖；
- 未显式 begin_batch（离线脚本直调）时首次上传自动按当前时间初始化；
- count_existing_runs 递归扫描：平铺旧文件 + 时间戳子目录双通道，序号严格递增。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import local_storage
from local_storage import begin_batch, current_batch_stamp, reset_batch, upload_file_to_storage


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    """把 local_storage 根目录指到 tmp_path，并重置批次状态（不碰真实 local_storage）。"""
    monkeypatch.setattr(local_storage, "LOCAL_STORAGE_DIR", str(tmp_path / "local_storage"))
    reset_batch()
    yield
    reset_batch()


def _touch(tmp_path, name: str, content: str = "x"):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


class TestBatchIsolation:
    def test_same_batch_shares_one_timestamp_dir(self, tmp_path):
        """同一次分析：PDF 与图表共享同一精确到秒的时间戳子目录。"""
        begin_batch("20260911_101530")
        pdf = _touch(tmp_path, "src.pdf")
        png = _touch(tmp_path, "src.png")
        url1 = upload_file_to_storage(pdf, "reports/中石油_2025_I_审计风险报告.pdf", "application/pdf")
        url2 = upload_file_to_storage(png, "charts/中石油_2025_I_风险热力图.png", "image/png")
        assert url1 == "/local_storage/20260911_101530/reports/中石油_2025_I_审计风险报告.pdf"
        assert url2 == "/local_storage/20260911_101530/charts/中石油_2025_I_风险热力图.png"
        # 两个文件真实落盘在同一批次目录下
        root = tmp_path / "local_storage" / "20260911_101530"
        assert (root / "reports" / "中石油_2025_I_审计风险报告.pdf").exists()
        assert (root / "charts" / "中石油_2025_I_风险热力图.png").exists()

    def test_next_batch_new_dir_old_files_kept(self, tmp_path):
        """下一次 begin_batch：生成全新时间戳目录，旧批次文件保留且不被覆盖。"""
        begin_batch("20260911_101530")
        upload_file_to_storage(_touch(tmp_path, "a.pdf"), "reports/A.pdf", "application/pdf")
        begin_batch("20260911_111530")
        upload_file_to_storage(_touch(tmp_path, "b.pdf"), "reports/A.pdf", "application/pdf")
        old = tmp_path / "local_storage" / "20260911_101530" / "reports" / "A.pdf"
        new = tmp_path / "local_storage" / "20260911_111530" / "reports" / "A.pdf"
        assert old.exists() and new.exists()
        # 同文件名落在不同批次目录，互不覆盖
        assert old.read_text(encoding="utf-8") == "x"
        assert new.read_text(encoding="utf-8") == "x"

    def test_auto_initialize_without_begin_batch(self, tmp_path):
        """未显式 begin_batch（离线脚本直调）：首次上传自动按当前时间初始化。"""
        url = upload_file_to_storage(_touch(tmp_path, "c.pdf"), "reports/C.pdf", "application/pdf")
        assert re.match(r"^/local_storage/\d{8}_\d{6}/reports/C\.pdf$", url), url
        assert (tmp_path / "local_storage").exists()

    def test_current_stamp_matches_upload(self, tmp_path):
        """current_batch_stamp 与上传目录一致（供脚本取当前批次）。"""
        stamp = begin_batch("20260911_121530")
        upload_file_to_storage(_touch(tmp_path, "d.pdf"), "reports/D.pdf", "application/pdf")
        assert current_batch_stamp() == "20260911_121530" == stamp

    def test_windows_separator_normalized(self, tmp_path):
        """Windows 下返回 URL 一律正斜杠，不出现反斜杠。"""
        begin_batch("20260911_131530")
        url = upload_file_to_storage(_touch(tmp_path, "e.pdf"), "reports/E.pdf", "application/pdf")
        assert "\\" not in url
        assert url.startswith("/local_storage/20260911_131530/reports/E.pdf")


class TestCountExistingRunsRecursive:
    def test_flat_and_timestamp_dirs_both_counted(self, tmp_path, monkeypatch):
        """递归扫描：平铺旧产物 _I 与时间戳子目录 _II 双通道，max=2。"""
        from datetime import datetime
        from utils.filename import count_existing_runs
        date_str = datetime.now().strftime("%Y%m%d")
        reports = tmp_path / "local_storage" / "reports"
        reports.mkdir(parents=True)
        (reports / f"{date_str}_中石油_2025_I_审计风险报告.pdf").write_text("")
        stamp_dir = tmp_path / "local_storage" / "20260911_101530" / "reports"
        stamp_dir.mkdir(parents=True)
        (stamp_dir / f"{date_str}_中石油_2025_II_审计底稿.xlsx").write_text("")
        _patch_filename_module_path(tmp_path, monkeypatch)
        assert count_existing_runs("中石油") == 2

    def test_subdir_deeper_than_one_level(self, tmp_path, monkeypatch):
        """递归深度>1 也能命中（reports/<stamp>/ 下再套一层）。"""
        from datetime import datetime
        from utils.filename import count_existing_runs
        date_str = datetime.now().strftime("%Y%m%d")
        deep = tmp_path / "local_storage" / "20260911_101530" / "reports" / "sub"
        deep.mkdir(parents=True)
        (deep / f"{date_str}_中石油_2025_III_审计风险报告.pdf").write_text("")
        _patch_filename_module_path(tmp_path, monkeypatch)
        assert count_existing_runs("中石油") == 3


def _patch_filename_module_path(tmp_path, monkeypatch):
    """把 utils.filename.__file__ 指向 tmp_path 下的伪路径，隔离真实 local_storage。"""
    fake_dir = tmp_path / "src" / "utils"
    fake_dir.mkdir(parents=True, exist_ok=True)
    import utils.filename as fn_mod
    monkeypatch.setattr(fn_mod, "__file__", str(fake_dir / "filename.py"))
    return fn_mod
