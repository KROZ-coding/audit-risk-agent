"""用户认证与分析历史服务层

为「多用户登录 + 各自分析历史」提供全部数据操作，零新增依赖：
- 口令安全：PBKDF2-HMAC-SHA256（120k 次迭代）+ 每用户独立随机盐，绝不存明文
- 会话令牌：secrets.token_hex(32) 随机串落库（session_tokens 表），
  服务重启不丢登录态，默认 7 天过期，登出即删除
- 历史隔离：analysis_history 按 user_id 过滤，接口层永远无法跨用户读取

所有函数自管理数据库会话（with get_session()），调用方无需关心事务；
失败路径返回 None / False 或抛出 ValueError（带用户可读信息），不泄露内部细节。
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from storage.database.db import get_session
from storage.database.shared.model import AnalysisHistory, SessionToken, User

logger = logging.getLogger(__name__)

# PBKDF2 迭代次数：本地单机场景下安全与登录延迟的平衡点
_PBKDF2_ITERATIONS = 120_000
# 会话令牌有效期（天）
_TOKEN_TTL_DAYS = 7
# 用户名规则：2-32 位中英文/数字/下划线，避免奇异字符引发展示或注入问题
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff]{2,32}$")


# ─── 口令哈希 ────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256 口令哈希，返回十六进制串。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()


# ─── 账号注册 / 登录 / 登出 ──────────────────────────────

def register_user(username: str, password: str) -> dict:
    """注册新用户。

    Args:
        username: 用户名（2-32 位中英文/数字/下划线）
        password: 口令（至少 6 位）

    Returns:
        {"user_id", "username"}

    Raises:
        ValueError: 用户名/口令不合规，或用户名已存在
    """
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("用户名需为 2-32 位中英文、数字或下划线")
    if not password or len(password) < 6:
        raise ValueError("口令长度至少 6 位")

    with get_session() as session:
        if session.query(User).filter(User.username == username).first():
            raise ValueError("用户名已存在")
        salt = secrets.token_hex(16)
        user = User(username=username, salt=salt, password_hash=_hash_password(password, salt))
        session.add(user)
        session.commit()
        logger.info(f"新用户注册: {username} (id={user.id})")
        return {"user_id": user.id, "username": user.username}


def login_user(username: str, password: str) -> dict | None:
    """校验口令并签发会话令牌。

    使用 hmac.compare_digest 恒时比较，避免时序侧信道。

    Returns:
        成功时 {"token", "user_id", "username"}；用户不存在或口令错误返回 None
        （两种失败对外不区分，防止用户名枚举）
    """
    username = (username or "").strip()
    with get_session() as session:
        user = session.query(User).filter(User.username == username).first()
        if user is None:
            return None
        if not hmac.compare_digest(user.password_hash, _hash_password(password or "", user.salt)):
            return None
        token = secrets.token_hex(32)
        session.add(SessionToken(
            token=token,
            user_id=user.id,
            expires_at=datetime.now() + timedelta(days=_TOKEN_TTL_DAYS),
        ))
        session.commit()
        return {"token": token, "user_id": user.id, "username": user.username}


def logout_user(token: str) -> bool:
    """删除会话令牌（幂等）。"""
    if not token:
        return False
    with get_session() as session:
        deleted = session.query(SessionToken).filter(SessionToken.token == token).delete()
        session.commit()
        return deleted > 0


def resolve_user(token: str) -> dict | None:
    """由会话令牌解析当前用户；令牌缺失/不存在/过期均返回 None。

    过期令牌顺手清理，避免 session_tokens 表无限膨胀。
    """
    if not token:
        return None
    with get_session() as session:
        st = session.query(SessionToken).filter(SessionToken.token == token).first()
        if st is None:
            return None
        if st.expires_at < datetime.now():
            session.delete(st)
            session.commit()
            return None
        user = session.get(User, st.user_id)
        if user is None:
            return None
        return {"user_id": user.id, "username": user.username}


# ─── 分析历史 ────────────────────────────────────────────

def save_history(user_id: int, run_id: str, *, company_name="", report_year="",
                 score=None, risk_level="", mode="pro", files=None, summary="",
                 report_snapshot=None, artifact_manifest=None, report_metadata=None,
                 ai_text="", data_status="", task_status="") -> int | None:
    """写入一条分析历史记录（仅登录用户调用；任何异常只记日志不阻断主流程）。

    Returns:
        新记录 id；失败返回 None
    """
    try:
        with get_session() as session:
            record = AnalysisHistory(
                user_id=user_id,
                run_id=run_id or "",
                company_name=(company_name or "")[:128],
                report_year=str(report_year or "")[:16],
                score=score,
                risk_level=(risk_level or "")[:16],
                mode=mode if mode in ("pro", "flash") else "pro",
                files_json=json.dumps(files or [], ensure_ascii=False),
                summary=(summary or "")[:1000],
                report_snapshot_json=json.dumps(report_snapshot or {}, ensure_ascii=False, default=str),
                artifact_manifest_json=json.dumps(artifact_manifest or [], ensure_ascii=False, default=str),
                report_metadata_json=json.dumps(report_metadata or {}, ensure_ascii=False, default=str),
                ai_text=(ai_text or "")[:100000],
                data_status=(data_status or "")[:32],
                task_status=(task_status or "")[:32],
            )
            session.add(record)
            session.commit()
            return record.id
    except Exception as e:  # noqa: BLE001 - 历史落库失败不应影响分析主流程
        logger.warning(f"分析历史落库失败（不影响主报告）: {e}")
        return None


def list_history(user_id: int, limit: int = 50) -> list[dict]:
    """按用户查询历史记录（倒序）。隔离保证：查询条件强制带 user_id。"""
    with get_session() as session:
        rows = (
            session.query(AnalysisHistory)
            .filter(AnalysisHistory.user_id == user_id)
            .order_by(AnalysisHistory.created_at.desc(), AnalysisHistory.id.desc())
            .limit(limit)
            .all()
        )
        return [_history_to_dict(r) for r in rows]


def get_history_detail(user_id: int, record_id: int) -> dict | None:
    """按 id 取单条历史，强制校验归属（他人记录返回 None，与不存在不区分）。"""
    with get_session() as session:
        r = session.get(AnalysisHistory, record_id)
        if r is None or r.user_id != user_id:
            return None
        return _history_to_dict(r)


def _history_to_dict(r: AnalysisHistory) -> dict:
    """ORM 记录转前端友好的 dict（files_json 解包为列表）。"""
    try:
        files = json.loads(r.files_json or "[]")
    except json.JSONDecodeError:
        files = []
    def _json_object(raw, fallback):
        try:
            value = json.loads(raw or "")
            return value if isinstance(value, type(fallback)) else fallback
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

    snapshot = _json_object(getattr(r, "report_snapshot_json", ""), {})
    manifest = _json_object(getattr(r, "artifact_manifest_json", ""), [])
    metadata = _json_object(getattr(r, "report_metadata_json", ""), {})
    return {
        "id": r.id,
        "run_id": r.run_id,
        "company_name": r.company_name,
        "report_year": r.report_year,
        "score": r.score,
        "risk_level": r.risk_level,
        "mode": r.mode,
        "files": files,
        "summary": r.summary,
        "snapshot_id": str(metadata.get("snapshot_id", "") or snapshot.get("snapshot_id", ""))
            if isinstance(metadata, dict) and isinstance(snapshot, dict) else "",
        "artifact_manifest": manifest,
        "report_metadata": metadata,
        "report_snapshot": snapshot,
        "ai_text": getattr(r, "ai_text", "") or "",
        "data_status": getattr(r, "data_status", "") or "",
        "task_status": getattr(r, "task_status", "") or "",
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
    }
