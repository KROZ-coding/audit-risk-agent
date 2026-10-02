"""定期回收模块的单元测试。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from maintenance import (
    MaintenancePolicy,
    prune_artifacts,
    prune_checkpoints,
    prune_logs,
    prune_temp,
    run_maintenance,
)


def _make_checkpoint_db(path: Path, thread_ids: list[str]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
        "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB)"
    )
    conn.execute(
        "CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
        "task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB)"
    )
    for i, tid in enumerate(thread_ids):
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, '', ?, NULL, 'json', ?, NULL)",
            (tid, f"c{i}", b"x" * 100),
        )
        conn.execute(
            "INSERT INTO writes VALUES (?, '', ?, 't', 0, 'c', 'json', ?)",
            (tid, f"c{i}", b"y" * 50),
        )
    conn.commit()
    conn.close()


def test_policy_defaults():
    p = MaintenancePolicy.from_env()
    assert p.enabled is True
    assert p.interval_hours == 24.0
    assert p.checkpoint_keep_threads == 50
    assert p.dry_run is False


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MAINTENANCE_INTERVAL_HOURS", "6")
    monkeypatch.setenv("MAINTENANCE_ENABLED", "false")
    monkeypatch.setenv("MAINTENANCE_CHECKPOINT_KEEP_THREADS", "3")
    p = MaintenancePolicy.from_env(project_dir=str(tmp_path))
    assert p.enabled is False
    assert p.interval_hours == 6.0
    assert p.checkpoint_keep_threads == 3


def test_prune_checkpoints_keeps_recent_threads(tmp_path):
    db = tmp_path / "checkpoints.sqlite"
    _make_checkpoint_db(db, [f"t{i}" for i in range(5)])
    p = MaintenancePolicy(
        project_dir=str(tmp_path), checkpoint_db=str(db),
        checkpoint_keep_threads=2, checkpoint_max_mb=0, checkpoint_vacuum=False,
    )
    result = prune_checkpoints(p, dry_run=False)
    assert result["deleted_thread_count"] == 3
    conn = sqlite3.connect(str(db))
    kept = {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    conn.close()
    assert kept == {"t3", "t4"}


def test_prune_checkpoints_keeps_session_group_together(tmp_path):
    db = tmp_path / "checkpoints.sqlite"
    _make_checkpoint_db(
        db,
        ["old-thread", "uuid-a-financial", "uuid-a-compliance", "uuid-a-synthesis"],
    )
    p = MaintenancePolicy(
        project_dir=str(tmp_path), checkpoint_db=str(db),
        checkpoint_keep_threads=1, checkpoint_max_mb=0, checkpoint_vacuum=False,
    )
    result = prune_checkpoints(p, dry_run=False)
    assert result["deleted_thread_count"] == 1
    conn = sqlite3.connect(str(db))
    kept = {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    conn.close()
    assert kept == {"uuid-a-financial", "uuid-a-compliance", "uuid-a-synthesis"}


def test_prune_checkpoints_dry_run_does_not_delete(tmp_path):
    db = tmp_path / "checkpoints.sqlite"
    _make_checkpoint_db(db, ["a", "b", "c"])
    p = MaintenancePolicy(
        project_dir=str(tmp_path), checkpoint_db=str(db),
        checkpoint_keep_threads=1, checkpoint_max_mb=0, checkpoint_vacuum=False,
    )
    result = prune_checkpoints(p, dry_run=True)
    assert result["candidate_thread_count"] == 2
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0]
    conn.close()
    assert count == 3


def test_prune_artifacts_keeps_min_batches(tmp_path):
    root = tmp_path / "local_storage"
    old = datetime.now() - timedelta(days=90)
    for name in ("20240101_000000", "20240102_000000", "20240103_000000"):
        d = root / name
        d.mkdir(parents=True)
        (d / "report.pdf").write_bytes(b"x" * 10)
    for d in root.iterdir():
        import os
        os.utime(d, (old.timestamp(), old.timestamp()))
    p = MaintenancePolicy(
        project_dir=str(tmp_path), artifact_dir=str(root),
        artifact_retention_days=30, artifact_min_batches=1,
    )
    result = prune_artifacts(p, dry_run=False)
    assert result["deleted_batch_count"] == 2
    remaining = sorted(x.name for x in root.iterdir() if x.is_dir())
    assert remaining == ["20240103_000000"]


def test_prune_temp_protects_pytest_dir(tmp_path):
    old = datetime.now() - timedelta(days=30)
    protected = tmp_path / ".tmp_pytest"
    protected.mkdir()
    stale = tmp_path / ".tmp_stale"
    stale.mkdir()
    import os
    for d in (protected, stale):
        os.utime(d, (old.timestamp(), old.timestamp()))
    p = MaintenancePolicy(project_dir=str(tmp_path), temp_retention_days=7)
    result = prune_temp(p, dry_run=False)
    assert result["deleted_count"] == 1
    assert protected.exists()
    assert not stale.exists()


def test_prune_logs_keeps_active_log(tmp_path):
    old = datetime.now() - timedelta(days=30)
    active = tmp_path / "app.log"
    active.write_text("active", encoding="utf-8")
    backup = tmp_path / "app.log.1"
    backup.write_text("old", encoding="utf-8")
    import os
    os.utime(backup, (old.timestamp(), old.timestamp()))
    p = MaintenancePolicy(project_dir=str(tmp_path), log_retention_days=14)
    result = prune_logs(p, dry_run=False)
    assert result["deleted_count"] == 1
    assert active.exists()
    assert not backup.exists()


def test_run_maintenance_reports_sections(tmp_path):
    p = MaintenancePolicy(
        project_dir=str(tmp_path), checkpoint_db=str(tmp_path / "missing.sqlite"),
        artifact_dir=str(tmp_path / "none"), checkpoint_max_mb=0,
    )
    report = run_maintenance(p, dry_run=True)
    assert report.dry_run is True
    assert set(report.to_dict()) >= {"checkpoints", "artifacts", "logs", "temp"}
