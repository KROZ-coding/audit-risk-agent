"""数据库预留基础设施冒烟测试

背景：db.py 为预留基础设施（业务主流程当前不落库，检查点由 checkpoints.sqlite
独立持久化）。本测试用于"保活"——证明预留件随时可启用而非死代码：
- get_db_url 的三级优先级（PGDATABASE_URL > 默认 SQLite）
- 引擎可对内存 SQLite 建连并执行探活 SQL
- ORM 元数据可完整建表（模型定义与引擎兼容）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import create_engine, text


class TestDbSmoke:
    """db.py 预留基础设施冒烟测试集"""

    def test_db_url_priority(self, monkeypatch):
        """PGDATABASE_URL 配置时优先返回；未配置时回退本地 SQLite 文件"""
        from storage.database.db import get_db_url

        monkeypatch.setenv("PGDATABASE_URL", "postgresql://u:p@localhost:5432/audit")
        assert get_db_url() == "postgresql://u:p@localhost:5432/audit"

        monkeypatch.delenv("PGDATABASE_URL", raising=False)
        url = get_db_url()
        assert url.startswith("sqlite:///") and url.endswith("local_data.db")

    def test_engine_connect_and_probe(self):
        """引擎能对内存 SQLite 建连并执行 SELECT 1 探活"""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1

    def test_orm_metadata_create_all(self):
        """ORM Base 元数据可在内存库完整建表（模型定义与引擎兼容）"""
        from storage.database.shared.model import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        # 再次建表应幂等不抛错
        Base.metadata.create_all(engine)
