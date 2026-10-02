"""运行时资源定期回收（maintenance）。

本模块解决长期运行/反复演示后的资源膨胀问题：

- ``checkpoints.sqlite``：LangGraph 会话检查点只增不减，单库可达 GB 级；
- ``local_storage/<批次>/``：每次分析生成的 PDF/Excel/图表永不删除；
- ``app.log.*``：RotatingFileHandler 的轮转备份；
- 项目根目录 ``.tmp_*``：开发/验收脚本留下的临时文件与目录。

设计原则：
1. **默认保守**：检查点库低于体积阈值时不删；始终保留最近 N 个活跃线程；
2. **可预览**：CLI 默认 dry-run；``--apply`` 才真正删除；
3. **可关闭**：``MAINTENANCE_ENABLED=false`` 完全停用后台线程；
4. **不误删当前状态**：活跃线程按数据库 rowid 的最近写入排序保留；
5. **失败不阻断主服务**：所有清理异常只记录日志，不影响 FastAPI 启动。

环境变量（均可选）：

============================  ==========================================
变量                          默认值
============================  ==========================================
MAINTENANCE_ENABLED           true
MAINTENANCE_INTERVAL_HOURS    24
MAINTENANCE_DRY_RUN           false
MAINTENANCE_CHECKPOINT_KEEP_THREADS  50
MAINTENANCE_CHECKPOINT_MAX_MB 512
MAINTENANCE_CHECKPOINT_VACUUM true
MAINTENANCE_ARTIFACT_RETENTION_DAYS  30
MAINTENANCE_ARTIFACT_MIN_BATCHES     10
MAINTENANCE_LOG_RETENTION_DAYS       14
MAINTENANCE_TEMP_RETENTION_DAYS      7
============================  ==========================================
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

BATCH_DIR_RE = re.compile(r"^\d{8}_\d{6}$")
TEMP_PREFIX = ".tmp_"
PROTECTED_TEMP_NAMES = {".tmp_pytest"}
CHECKPOINT_DB_NAME = "checkpoints.sqlite"

# 最近一次回收报告（供 /api/maintenance/status 查询）
_LAST_REPORT: dict[str, Any] = {}


# ─────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw.strip()))
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，使用默认值 %s", name, raw, default)
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, float(raw.strip()))
    except ValueError:
        logger.warning("环境变量 %s=%r 不是数字，使用默认值 %s", name, raw, default)
        return default


@dataclass
class MaintenancePolicy:
    """回收策略。所有阈值都可以通过环境变量覆盖。"""

    enabled: bool = True
    interval_hours: float = 24.0
    dry_run: bool = False
    project_dir: str = ""
    checkpoint_db: str = ""
    checkpoint_keep_threads: int = 50
    checkpoint_max_mb: int = 512
    checkpoint_vacuum: bool = True
    artifact_dir: str = ""
    artifact_retention_days: int = 30
    artifact_min_batches: int = 10
    log_retention_days: int = 14
    temp_retention_days: int = 7

    @classmethod
    def from_env(cls, project_dir: str | None = None) -> "MaintenancePolicy":
        root = Path(project_dir or os.getcwd()).resolve()
        checkpoint = os.getenv("CHECKPOINT_DB_PATH") or str(root / CHECKPOINT_DB_NAME)
        return cls(
            enabled=_env_bool("MAINTENANCE_ENABLED", True),
            interval_hours=_env_float("MAINTENANCE_INTERVAL_HOURS", 24.0, 0.1),
            dry_run=_env_bool("MAINTENANCE_DRY_RUN", False),
            project_dir=str(root),
            checkpoint_db=checkpoint,
            checkpoint_keep_threads=_env_int("MAINTENANCE_CHECKPOINT_KEEP_THREADS", 50, 1),
            checkpoint_max_mb=_env_int("MAINTENANCE_CHECKPOINT_MAX_MB", 512, 0),
            checkpoint_vacuum=_env_bool("MAINTENANCE_CHECKPOINT_VACUUM", True),
            artifact_dir=str(root / "local_storage"),
            artifact_retention_days=_env_int("MAINTENANCE_ARTIFACT_RETENTION_DAYS", 30, 0),
            artifact_min_batches=_env_int("MAINTENANCE_ARTIFACT_MIN_BATCHES", 10, 0),
            log_retention_days=_env_int("MAINTENANCE_LOG_RETENTION_DAYS", 14, 0),
            temp_retention_days=_env_int("MAINTENANCE_TEMP_RETENTION_DAYS", 7, 0),
        )

    def resolved_checkpoint_db(self) -> Path:
        path = Path(self.checkpoint_db)
        if not path.is_absolute():
            path = Path(self.project_dir) / path
        return path.resolve()


# ─────────────────────────────────────────────────────────
# 报告
# ─────────────────────────────────────────────────────────

@dataclass
class MaintenanceReport:
    dry_run: bool
    started_at: str
    duration_sec: float = 0.0
    checkpoints: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    logs: dict[str, Any] = field(default_factory=dict)
    temp: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        c = self.checkpoints
        a = self.artifacts
        lines = [
            f"回收{'预览' if self.dry_run else '结果'}（{self.duration_sec:.2f}s）",
            f"  检查点: 删除线程 {c.get('deleted_thread_count', 0)} / 候选 {c.get('candidate_thread_count', 0)}"
            + (f"，可释放约 {c.get('candidate_mb', 0)} MB" if c.get('candidate_mb') else "")
            + (f"，库体积 {c.get('size_mb_before')} → {c.get('size_mb_after')} MB"
               if c.get('size_mb_after') is not None else ""),
            f"  产物批次: 删除 {a.get('deleted_batch_count', 0)} 个，释放约 {a.get('deleted_mb', 0)} MB",
            f"  日志备份: 删除 {self.logs.get('deleted_count', 0)} 个",
            f"  临时文件: 删除 {self.temp.get('deleted_count', 0)} 个",
        ]
        if self.errors:
            lines.append(f"  错误: {len(self.errors)} 条（详见 JSON 报告）")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# 检查点
# ─────────────────────────────────────────────────────────

def _checkpoint_threads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """返回按最近活跃度（rowid）降序排列的线程统计。"""
    stats: dict[str, dict[str, Any]] = {}
    for tid, size, last_rowid in conn.execute(
        "SELECT thread_id, COALESCE(SUM(LENGTH(checkpoint)), 0), MAX(rowid) "
        "FROM checkpoints GROUP BY thread_id"
    ):
        stats[str(tid)] = {"thread_id": str(tid), "bytes": int(size or 0), "last_rowid": int(last_rowid or 0)}
    for tid, last_rowid in conn.execute(
        "SELECT thread_id, MAX(rowid) FROM writes GROUP BY thread_id"
    ):
        tid = str(tid)
        if tid in stats:
            stats[tid]["last_rowid"] = max(stats[tid]["last_rowid"], int(last_rowid or 0))
        else:
            stats[tid] = {"thread_id": tid, "bytes": 0, "last_rowid": int(last_rowid or 0)}
    return sorted(stats.values(), key=lambda item: item["last_rowid"], reverse=True)


def _session_key(thread_id: str) -> str:
    """将 ``<uuid>-financial`` 等线程归并到同一会话键，普通线程原样返回。"""
    for suffix in ("-financial", "-compliance", "-synthesis"):
        if thread_id.endswith(suffix):
            return thread_id[: -len(suffix)]
    return thread_id


def prune_checkpoints(policy: MaintenancePolicy, dry_run: Optional[bool] = None) -> dict[str, Any]:
    """按「最近 N 个线程 + 体积阈值」回收检查点。"""
    dry_run = policy.dry_run if dry_run is None else dry_run
    db_path = policy.resolved_checkpoint_db()
    result: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "dry_run": dry_run,
        "deleted_thread_count": 0,
        "candidate_thread_count": 0,
        "candidate_mb": 0.0,
        "deleted_threads": [],
    }
    if str(db_path) == ":memory:" or not db_path.exists():
        result["skipped"] = "检查点数据库不存在"
        return result

    size_before = db_path.stat().st_size
    result["size_mb_before"] = round(size_before / (1024 * 1024), 2)
    if policy.checkpoint_max_mb > 0 and result["size_mb_before"] <= policy.checkpoint_max_mb:
        result["skipped"] = f"库体积 {result['size_mb_before']} MB 未超过阈值 {policy.checkpoint_max_mb} MB"
        return result

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        threads = _checkpoint_threads(conn)
        # 按会话分组保留：同一 UUID 的 financial/compliance/synthesis 必须同生共死，
        # 否则会留下无法续跑的残缺会话。组按组内最新 rowid 排序。
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in threads:
            groups.setdefault(_session_key(item["thread_id"]), []).append(item)
        ordered_groups = sorted(
            groups.values(),
            key=lambda items: max(item["last_rowid"] for item in items),
            reverse=True,
        )
        kept_groups = ordered_groups[: policy.checkpoint_keep_threads]
        keep = {item["thread_id"] for group in kept_groups for item in group}
        candidates = [item for item in threads if item["thread_id"] not in keep]
        result["total_thread_count"] = len(threads)
        result["kept_thread_count"] = len(keep)
        result["candidate_thread_count"] = len(candidates)
        result["candidate_mb"] = round(sum(item["bytes"] for item in candidates) / (1024 * 1024), 2)
        result["deleted_threads"] = [item["thread_id"] for item in candidates]
        if dry_run or not candidates:
            return result

        conn.execute("BEGIN IMMEDIATE")
        for item in candidates:
            conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (item["thread_id"],))
            conn.execute("DELETE FROM writes WHERE thread_id = ?", (item["thread_id"],))
        conn.commit()
        result["deleted_thread_count"] = len(candidates)
        if policy.checkpoint_vacuum:
            try:
                conn.execute("VACUUM")
                result["vacuumed"] = True
            except sqlite3.OperationalError as exc:  # 运行中连接可能阻止 VACUUM
                result["vacuumed"] = False
                result["vacuum_error"] = str(exc)
        result["size_mb_after"] = round(db_path.stat().st_size / (1024 * 1024), 2)
    except sqlite3.OperationalError as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - 回收失败不应中断服务
        result["error"] = str(exc)
    finally:
        if conn is not None:
            conn.close()
    return result


# ─────────────────────────────────────────────────────────
# 产物 / 日志 / 临时文件
# ─────────────────────────────────────────────────────────

def _safe_rmtree(path: Path) -> int:
    """删除目录并返回释放的字节数。失败时返回 0。"""
    try:
        size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        shutil.rmtree(path, ignore_errors=False)
        return size
    except OSError as exc:
        logger.warning("删除目录失败 %s: %s", path, exc)
        return 0


def prune_artifacts(policy: MaintenancePolicy, dry_run: Optional[bool] = None) -> dict[str, Any]:
    """回收 local_storage 下的旧批次目录与旧报告/图表文件。"""
    dry_run = policy.dry_run if dry_run is None else dry_run
    root = Path(policy.artifact_dir)
    result: dict[str, Any] = {
        "path": str(root),
        "exists": root.exists(),
        "dry_run": dry_run,
        "deleted_batch_count": 0,
        "deleted_mb": 0.0,
        "deleted_batches": [],
        "deleted_legacy_count": 0,
    }
    if not root.exists():
        result["skipped"] = "产物目录不存在"
        return result

    cutoff = datetime.now() - timedelta(days=policy.artifact_retention_days)
    batches = sorted(
        (p for p in root.iterdir() if p.is_dir() and BATCH_DIR_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    keep = {p.name for p in batches[: policy.artifact_min_batches]}
    candidates = []
    for path in batches:
        if path.name in keep:
            continue
        if datetime.fromtimestamp(path.stat().st_mtime) >= cutoff:
            continue
        candidates.append(path)

    result["candidate_batch_count"] = len(candidates)
    result["candidate_mb"] = round(
        sum(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in candidates) / (1024 * 1024), 2
    )
    result["deleted_batches"] = [p.name for p in candidates]
    if not dry_run:
        freed = 0
        for path in candidates:
            freed += _safe_rmtree(path)
        result["deleted_batch_count"] = sum(1 for p in candidates if not p.exists())
        result["deleted_mb"] = round(freed / (1024 * 1024), 2)

    # 旧版平铺 reports/ charts/ 文件（批次改造前的历史数据）同样按时间回收
    legacy_cutoff_ts = cutoff.timestamp()
    for sub in ("reports", "charts"):
        sub_dir = root / sub
        if not sub_dir.is_dir():
            continue
        for path in sub_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime >= legacy_cutoff_ts:
                    continue
            except OSError:
                continue
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("删除产物失败 %s: %s", path, exc)
                    continue
            result["deleted_legacy_count"] += 1
    return result


def prune_logs(policy: MaintenancePolicy, dry_run: Optional[bool] = None) -> dict[str, Any]:
    """回收 RotatingFileHandler 的旧备份日志（不删除 app.log 本身）。"""
    dry_run = policy.dry_run if dry_run is None else dry_run
    root = Path(policy.project_dir)
    cutoff_ts = time.time() - policy.log_retention_days * 86400
    result: dict[str, Any] = {"path": str(root), "dry_run": dry_run, "deleted_count": 0, "deleted": []}
    for path in sorted(root.glob("app.log.*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff_ts:
                continue
        except OSError:
            continue
        result["deleted"].append(path.name)
        if not dry_run:
            try:
                path.unlink()
                result["deleted_count"] += 1
            except OSError as exc:
                logger.warning("删除日志失败 %s: %s", path, exc)
    return result


def prune_temp(policy: MaintenancePolicy, dry_run: Optional[bool] = None) -> dict[str, Any]:
    """回收项目根目录下过期的 ``.tmp_*`` 临时文件/目录。"""
    dry_run = policy.dry_run if dry_run is None else dry_run
    root = Path(policy.project_dir)
    cutoff_ts = time.time() - policy.temp_retention_days * 86400
    result: dict[str, Any] = {"path": str(root), "dry_run": dry_run, "deleted_count": 0, "deleted": []}
    for path in sorted(root.glob(f"{TEMP_PREFIX}*")):
        if path.name in PROTECTED_TEMP_NAMES:
            continue
        try:
            if path.stat().st_mtime >= cutoff_ts:
                continue
        except OSError:
            continue
        result["deleted"].append(path.name)
        if dry_run:
            continue
        try:
            if path.is_dir():
                _safe_rmtree(path)
            else:
                path.unlink()
            if not path.exists():
                result["deleted_count"] += 1
        except OSError as exc:
            logger.warning("删除临时文件失败 %s: %s", path, exc)
    return result


# ─────────────────────────────────────────────────────────
# 编排
# ─────────────────────────────────────────────────────────

def run_maintenance(
    policy: Optional[MaintenancePolicy] = None,
    dry_run: Optional[bool] = None,
    sections: Optional[Iterable[str]] = None,
) -> MaintenanceReport:
    """执行一次回收，返回结构化报告。任何单节失败都不抛出。"""
    policy = policy or MaintenancePolicy.from_env()
    dry_run = policy.dry_run if dry_run is None else dry_run
    selected = set(sections or ("checkpoints", "artifacts", "logs", "temp"))
    report = MaintenanceReport(
        dry_run=dry_run,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    started = time.perf_counter()
    runners = {
        "checkpoints": ("checkpoints", prune_checkpoints),
        "artifacts": ("artifacts", prune_artifacts),
        "logs": ("logs", prune_logs),
        "temp": ("temp", prune_temp),
    }
    for key in ("checkpoints", "artifacts", "logs", "temp"):
        if key not in selected:
            continue
        attr, runner = runners[key]
        try:
            setattr(report, attr, runner(policy, dry_run))
        except Exception as exc:  # noqa: BLE001
            logger.warning("回收节 %s 失败: %s", key, exc)
            report.errors.append(f"{key}: {exc}")
            setattr(report, attr, {"error": str(exc)})
    report.duration_sec = round(time.perf_counter() - started, 2)
    global _LAST_REPORT
    _LAST_REPORT = report.to_dict()
    return report


def last_report() -> dict[str, Any]:
    return dict(_LAST_REPORT)


async def maintenance_worker(policy: Optional[MaintenancePolicy] = None) -> None:
    """后台周期回收协程；由 FastAPI lifespan 启动，取消即退出。"""
    policy = policy or MaintenancePolicy.from_env()
    if not policy.enabled:
        logger.info("定期回收已关闭（MAINTENANCE_ENABLED=false）")
        return
    # 启动后先等待，让服务完成初始化，避免与首屏请求争抢 IO。
    try:
        await asyncio.sleep(min(60.0, max(5.0, policy.interval_hours * 60.0)))
        while True:
            try:
                report = await asyncio.to_thread(run_maintenance, policy, policy.dry_run)
                logger.info("定期回收完成:\n%s", report.summary())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("定期回收执行失败: %s", exc)
            await asyncio.sleep(max(60.0, policy.interval_hours * 3600.0))
    except asyncio.CancelledError:
        logger.info("定期回收线程已停止")
        raise
