"""多用户认证与分析历史功能测试

覆盖：
- 注册/登录/登出/令牌解析（含口令错误、重名、过期令牌）
- 历史落库与按用户隔离（A 的记录 B 永远查不到，详情越权返回 None/404）
- API 层：未登录 401、注册→分析历史列表全链路
- _extract_history_fields 从最终报告抽取公司/评分/文件字段

隔离策略：每个用例通过 isolated_db fixture 使用独立临时 SQLite，
重置 db.py 的引擎/会话工厂全局缓存，测试间互不污染、也不碰真实 local_data.db。
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import storage.database.db as db_mod
from storage.database.shared.model import Base, SessionToken


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """独立临时业务库：重置引擎缓存 + 指向 tmp SQLite + 建表。"""
    db_url = f"sqlite:///{(tmp_path / 'biz_test.db').as_posix()}"
    monkeypatch.setenv("PGDATABASE_URL", db_url)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionLocal", None)
    Base.metadata.create_all(db_mod.get_engine())
    yield


class TestAuthService:
    """user_service 认证层：注册/登录/令牌生命周期"""

    def test_register_login_resolve_logout(self, isolated_db):
        from storage.database.user_service import (
            login_user, logout_user, register_user, resolve_user,
        )
        info = register_user("审计员甲", "secret123")
        assert info["username"] == "审计员甲"

        session_info = login_user("审计员甲", "secret123")
        assert session_info and session_info["user_id"] == info["user_id"]

        user = resolve_user(session_info["token"])
        assert user == {"user_id": info["user_id"], "username": "审计员甲"}

        assert logout_user(session_info["token"]) is True
        assert resolve_user(session_info["token"]) is None  # 登出后令牌失效

    def test_wrong_password_and_unknown_user(self, isolated_db):
        from storage.database.user_service import login_user, register_user
        register_user("user_a", "secret123")
        # 口令错误与用户不存在均返回 None（对外不区分，防枚举）
        assert login_user("user_a", "wrong-pass") is None
        assert login_user("no_such_user", "secret123") is None

    def test_duplicate_username_and_invalid_input(self, isolated_db):
        from storage.database.user_service import register_user
        register_user("user_b", "secret123")
        with pytest.raises(ValueError):
            register_user("user_b", "another123")     # 重名
        with pytest.raises(ValueError):
            register_user("x", "secret123")           # 用户名过短
        with pytest.raises(ValueError):
            register_user("user_c", "123")            # 口令过短

    def test_expired_token_rejected_and_cleaned(self, isolated_db):
        from storage.database.user_service import login_user, register_user, resolve_user
        register_user("user_d", "secret123")
        session_info = login_user("user_d", "secret123")
        # 手动将令牌改为已过期
        with db_mod.get_session() as s:
            st = s.get(SessionToken, session_info["token"])
            st.expires_at = datetime.now() - timedelta(seconds=1)
            s.commit()
        assert resolve_user(session_info["token"]) is None
        # 过期令牌应被顺手清理
        with db_mod.get_session() as s:
            assert s.get(SessionToken, session_info["token"]) is None

    def test_password_not_stored_in_plaintext(self, isolated_db):
        from storage.database.shared.model import User
        from storage.database.user_service import register_user
        register_user("user_e", "secret123")
        with db_mod.get_session() as s:
            u = s.query(User).filter(User.username == "user_e").first()
            assert "secret123" not in u.password_hash
            assert len(u.password_hash) == 64 and len(u.salt) == 32  # sha256 hex + 16字节盐 hex


class TestHistoryIsolation:
    """分析历史：落库、按用户隔离、越权防护"""

    def _register(self, name):
        from storage.database.user_service import login_user, register_user
        register_user(name, "secret123")
        return login_user(name, "secret123")

    def test_history_isolated_between_users(self, isolated_db):
        from storage.database.user_service import get_history_detail, list_history, save_history
        ua = self._register("user_甲")
        ub = self._register("user_乙")

        rid = save_history(ua["user_id"], "run-a1", company_name="茅台", score=15.0,
                           risk_level="低风险", files=["/local_storage/reports/a.pdf"])
        save_history(ub["user_id"], "run-b1", company_name="乐视网", score=88.0, risk_level="极高风险")

        # 各自只能看到自己的记录
        a_list = list_history(ua["user_id"])
        b_list = list_history(ub["user_id"])
        assert [r["company_name"] for r in a_list] == ["茅台"]
        assert [r["company_name"] for r in b_list] == ["乐视网"]
        # 越权取详情：乙拿甲的记录 id → None（与不存在不区分）
        assert get_history_detail(ub["user_id"], rid) is None
        assert get_history_detail(ua["user_id"], rid)["files"] == ["/local_storage/reports/a.pdf"]

    def test_history_order_and_limit(self, isolated_db):
        from storage.database.user_service import list_history, save_history
        u = self._register("user_丙")
        for i in range(3):
            save_history(u["user_id"], f"run-{i}", company_name=f"公司{i}")
        records = list_history(u["user_id"], limit=2)
        # 倒序 + limit 生效
        assert len(records) == 2
        assert records[0]["company_name"] == "公司2"


class TestHistoryFieldExtraction:
    """main._extract_history_fields 从最终报告抽取结构化字段"""

    def test_extracts_company_score_and_files(self):
        from main import _extract_history_fields
        ai_text = (
            '风险台账：{"company_info": {"company_name": "测试公司", "report_year": "2025"}, '
            '"risk_details": []}\n\n'
            '<!--COMPREHENSIVE_SCORE-->\n'
            '{"score": 42.5, "level": "中等风险", "level_key": "medium", "summary": "存在一定风险信号"}'
        )
        report = {
            "ai_text": ai_text,
            "files": [{"path": "/local_storage/reports/x.pdf"}],
            "images": [{"path": "/local_storage/charts/y.png"}],
        }
        fields = _extract_history_fields(report)
        assert fields["company_name"] == "测试公司"
        assert fields["report_year"] == "2025"
        assert fields["score"] == 42.5
        assert fields["risk_level"] == "中等风险"
        assert fields["files"] == ["/local_storage/reports/x.pdf", "/local_storage/charts/y.png"]

    def test_degrades_gracefully_on_missing_data(self):
        from main import _extract_history_fields
        fields = _extract_history_fields({"ai_text": "纯文本回复，无结构化数据"})
        assert fields["company_name"] == "" and fields["score"] is None and fields["files"] == []


class TestAuthHistoryApi:
    """API 层全链路（TestClient，不进入 lifespan）"""

    def test_register_login_and_history_flow(self, isolated_db):
        from fastapi.testclient import TestClient
        import main as main_mod
        client = TestClient(main_mod.app)

        # 未登录查历史 → 401
        assert client.get("/api/history").status_code == 401

        # 注册并自动登录
        resp = client.post("/api/auth/register", json={"username": "api_user", "password": "secret123"})
        assert resp.status_code == 200
        token = resp.json()["token"]
        headers = {"X-Auth-Token": token}

        # me 接口识别登录态
        me = client.get("/api/auth/me", headers=headers).json()
        assert me["status"] == "ok" and me["username"] == "api_user"

        # 直接落一条历史后可查到
        from storage.database.user_service import save_history
        save_history(me["user_id"], "run-api", company_name="API公司", score=30.0, risk_level="中等风险")
        data = client.get("/api/history", headers=headers).json()
        assert data["status"] == "ok" and data["records"][0]["company_name"] == "API公司"

        # 错误口令登录 → 401；登出后 me 回到游客态
        assert client.post("/api/auth/login", json={"username": "api_user", "password": "bad"}).status_code == 401
        client.post("/api/auth/logout", headers=headers)
        assert client.get("/api/auth/me", headers=headers).json()["status"] == "anonymous"
