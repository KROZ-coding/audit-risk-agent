"""上传安全与鉴权中间件的回归测试

锁定 v3.1 的三项 P3 安全修复，防止回退：
1. /upload 路径穿越：恶意文件名（含 ../ 或盘符路径）必须被净化，落盘文件
   始终位于 UPLOAD_DIR 内；
2. X-API-Key 最小鉴权：配置 APP_API_KEY 后写操作接口未带头返回 401、带头放行，
   未配置时行为与从前完全一致（零摩擦）；
3. 非保护接口（/health）不受鉴权影响。

注：TestClient 不进入 lifespan 上下文（避免构建真实 Agent / LLM 实例），
仅覆盖路由与中间件层行为。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

import main as main_mod


client = TestClient(main_mod.app)


class TestUploadPathTraversal:
    """/upload 文件名净化：路径成分剥离 + 非法字符替换"""

    def _upload(self, filename: str):
        return client.post(
            "/upload",
            files=[("files", (filename, b"hello audit", "text/plain"))],
        )

    def test_dotdot_filename_stays_inside_upload_dir(self):
        resp = self._upload("../../../evil.txt")
        assert resp.status_code == 200
        info = resp.json()["files"][0]
        assert info["status"] == "ok"
        saved = os.path.realpath(info["saved_path"])
        upload_root = os.path.realpath(main_mod.UPLOAD_DIR)
        # 落盘路径必须仍在 UPLOAD_DIR 内（穿越成分已被 basename+净化剥掉）
        assert saved.startswith(upload_root + os.sep)
        assert ".." not in os.path.basename(saved)

    def test_windows_style_traversal_sanitized(self):
        resp = self._upload("..\\..\\Windows\\Temp\\evil.txt")
        assert resp.status_code == 200
        info = resp.json()["files"][0]
        saved = os.path.realpath(info["saved_path"])
        upload_root = os.path.realpath(main_mod.UPLOAD_DIR)
        assert saved.startswith(upload_root + os.sep)
        # 反斜杠等分隔符被替换为下划线，不产生子目录
        assert os.path.dirname(saved) == upload_root


class TestApiKeyGuard:
    """X-API-Key 鉴权中间件：默认关闭、配置后仅拦截写操作接口"""

    def test_no_key_configured_allows_all(self, monkeypatch):
        monkeypatch.delenv("APP_API_KEY", raising=False)
        resp = client.post(
            "/upload", files=[("files", ("a.txt", b"x", "text/plain"))]
        )
        assert resp.status_code == 200

    def test_protected_route_requires_key(self, monkeypatch):
        monkeypatch.setenv("APP_API_KEY", "secret-key")
        # 未带头 → 401
        resp = client.post(
            "/upload", files=[("files", ("a.txt", b"x", "text/plain"))]
        )
        assert resp.status_code == 401
        # 带错误头 → 401
        resp = client.post(
            "/upload",
            files=[("files", ("a.txt", b"x", "text/plain"))],
            headers={"X-API-Key": "wrong"},
        )
        assert resp.status_code == 401
        # 带正确头 → 放行
        resp = client.post(
            "/upload",
            files=[("files", ("a.txt", b"x", "text/plain"))],
            headers={"X-API-Key": "secret-key"},
        )
        assert resp.status_code == 200

    def test_unprotected_route_bypasses_guard(self, monkeypatch):
        monkeypatch.setenv("APP_API_KEY", "secret-key")
        # /health 不在保护前缀内，无需鉴权
        resp = client.get("/health")
        assert resp.status_code == 200
