# -*- coding: utf-8 -*-
"""\u542f\u52a8\u7aef\u53e3\u81ea\u6108\u56de\u5f52\u6d4b\u8bd5\u3002

\u80cc\u666f\uff1aWindows \u4e0a\u52a0\u901f\u5668/\u4ee3\u7406\u8f6f\u4ef6\uff08\u5982 Watt Toolkit / Steam++\uff09\u4f1a\u4ee5 \u300cBound\u300d
\u72b6\u6001\u957f\u671f\u5360\u7528 5000 \u7b49\u7aef\u53e3\uff0c\u65e7\u9884\u68c0\u53ea\u67e5 Get-NetTCPConnection -State Listen
\u62b3\u4e0d\u5230\u8fd9\u79cd\u5360\u7528\uff0c\u5bfc\u81f4 uvicorn \u62a5 WinError 10013 \u9000\u51fa\u3001\u5e94\u7528\u65e0\u6cd5\u542f\u52a8\u3002
\u672c\u6d4b\u8bd5\u9501\u5b9a\u4e24\u4e2a\u884c\u4e3a\uff1a

1. `main._pick_free_port` \u5bf9\u76ee\u6807\u7aef\u53e3\u505a\u771f\u5b9e bind \u63a2\u6d4b\uff08\u540c\u65f6\u8986\u76d6 127.0.0.1 \u4e0e
   0.0.0.0\uff09\uff0c\u88ab\u5360\u7528\u65f6\u5411\u540e\u987a\u5ef6\u5230\u7a7a\u95f2\u7aef\u53e3\uff0c\u907f\u514d\u642c\u5230\u5929\u70b9\u3002
2. `start.ps1` \u4fdd\u7559\u300c\u771f\u5b9e bind \u63a2\u6d4b + \u81ea\u52a8\u6539\u7528\u7a7a\u95f2\u7aef\u53e3\u300d\u7684\u542f\u52a8\u524d\u81ea\u6108\u7f16\u8f91\uff0c
   \u5e76\u4e0d\u518d\u4f9d\u8d56\u53ea\u80fd\u67e5 Listen \u72b6\u6001\u7684\u65e7\u903b\u8f91\u3002
"""
import os
import socket

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
START_PS1 = os.path.join(PROJECT_ROOT, "start.ps1")


def _ephemeral_free_port():
    """\u5411\u7cfb\u7edf\u8981\u4e00\u4e2a\u5f53\u524d\u7a7a\u95f2\u7684\u7aef\u53e3\u53f7\uff08bind :0 \u540e\u7acb\u523b\u91ca\u653e\uff09\u3002"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _occupy(host, port):
    """\u5728\u7ed9\u5b9a\u5730\u5740\u5360\u4f4f\u7aef\u53e3\uff0c\u8fd4\u56de\u9700\u8981\u5173\u95ed\u7684 socket\u3002"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, port))
    s.listen(1)
    return s


def test_pick_free_port_returns_requested_port_when_free():
    from main import _pick_free_port

    port = _ephemeral_free_port()
    assert _pick_free_port("127.0.0.1", port) == port


def test_pick_free_port_skips_port_bound_on_loopback():
    """\u56de\u73af\u5730\u5740\u88ab\u5360\uff08\u670d\u52a1\u672c\u8eab\u5df2\u5728\u8dd1\uff09\u65f6\u5e94\u5411\u540e\u987a\u5ef6\u3002"""
    from main import _pick_free_port

    port = _ephemeral_free_port()
    blocker = _occupy("127.0.0.1", port)
    try:
        picked = _pick_free_port("127.0.0.1", port)
        assert picked != port
        assert port < picked <= port + 20
    finally:
        blocker.close()


def test_pick_free_port_skips_port_bound_on_all_interfaces():
    """\u6a21\u62df Watt Toolkit/Steam++\uff1a\u7aef\u53e3\u88ab 0.0.0.0 \u5168\u63a5\u53e3\u5360\u7528\u65f6\u4e5f\u80fd\u8bc6\u522b\u5e76\u907f\u8ba9\u3002"""
    from main import _pick_free_port

    port = _ephemeral_free_port()
    blocker = _occupy("0.0.0.0", port)
    try:
        picked = _pick_free_port("127.0.0.1", port)
        assert picked != port, "\u5168\u63a5\u53e3\u88ab\u5360\u65f6\u4e0d\u5e94\u91cd\u590d\u9009\u4e2d\u540c\u4e00\u7aef\u53e3"
    finally:
        blocker.close()


def test_pick_free_port_falls_back_when_range_exhausted():
    """\u5019\u9009\u533a\u95f4\u5df2\u7a77\u5c3d\u65f6\u4fdd\u6301\u539f\u7aef\u53e3\u8fd4\u56de\uff0c\u7531\u4e0a\u5c42\u7ed9\u51fa\u660e\u786e\u62a5\u9519\u3002"""
    from main import _pick_free_port

    port = _ephemeral_free_port()
    blocker = _occupy("127.0.0.1", port)
    try:
        # attempts=0 \u610f\u5473\u7740\u53ea\u68c0\u67e5\u8be5\u7aef\u53e3\u672c\u8eab\uff1b\u88ab\u5360\u65f6\u5e94\u56de\u9000\u539f\u503c\u3002
        assert _pick_free_port("127.0.0.1", port, attempts=0) == port
    finally:
        blocker.close()


def test_start_script_contains_port_self_heal_logic():
    """\u9632\u6b62\u6709\u4eba\u628a start.ps1 \u7684\u7aef\u53e3\u81ea\u6108\u903b\u8f91\u6539\u56de\u53ea\u67e5 Listen \u7684\u65e7\u5b9e\u73b0\u3002"""
    with open(START_PS1, encoding="utf-8-sig") as fh:
        text = fh.read()

    assert "function Test-PortFree" in text, "start.ps1 \u5e94\u4fdd\u7559\u771f\u5b9e bind \u63a2\u6d4b\u51fd\u6570"
    assert "$listener.Start()" in text, "\u7aef\u53e3\u63a2\u6d4b\u5fc5\u987b\u771f\u6b63 bind\uff0c\u800c\u4e0d\u662f\u53ea\u770b Listen \u72b6\u6001"
    assert "\u5df2\u81ea\u52a8\u6539\u7528\u7a7a\u95f2\u7aef\u53e3" in text, "start.ps1 \u5e94\u5728\u7aef\u53e3\u88ab\u5360\u65f6\u81ea\u52a8\u6539\u7528\u7a7a\u95f2\u7aef\u53e3"
