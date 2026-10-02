"""业务数据库 ORM 模型定义

三张表支撑「多用户登录 + 各自分析历史」功能：
- User：用户账号（PBKDF2 加盐哈希存储口令，绝不存明文）
- SessionToken：登录会话令牌（随机 token 落库，重启不丢登录态，支持过期）
- AnalysisHistory：分析历史记录（按 user_id 隔离，游客分析不落历史）

表结构由 db.init_tables() 在服务启动时幂等创建（Base.metadata.create_all）。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    """用户账号表。password_hash 为 PBKDF2-HMAC-SHA256(口令, salt) 的十六进制串。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class SessionToken(Base):
    """登录会话令牌表。token 为 secrets.token_hex 随机串，过期后校验失败。"""

    __tablename__ = "session_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AnalysisHistory(Base):
    """分析历史记录表：一次完整审计分析的结构化摘要，按用户隔离查询。"""

    __tablename__ = "analysis_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    company_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    report_year: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=True)          # 综合风险分（0-100，可能缺失）
    risk_level: Mapped[str] = mapped_column(String(16), default="", nullable=False)  # 低/中等/高/极高风险
    mode: Mapped[str] = mapped_column(String(8), default="pro", nullable=False)      # pro / flash
    files_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)      # 报告/图表路径列表(JSON)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)           # 一句话结论摘要
    # 新快照链路：历史列表使用 metadata/manifest，详情可恢复完整快照。
    # 使用 JSON 文本而非数据库方言 JSON，兼容 SQLite 和 PostgreSQL。
    report_snapshot_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    artifact_manifest_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    report_metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    ai_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data_status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    task_status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True, nullable=False)
