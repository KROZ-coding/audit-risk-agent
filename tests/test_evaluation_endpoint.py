"""评估接口回归：API 与命令行共用快速工具评估实现。"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_tool_evaluation_endpoint_returns_metrics_quickly():
    from main import run_evaluation

    started = time.perf_counter()
    result = asyncio.run(run_evaluation("tool"))
    elapsed = time.perf_counter() - started

    assert elapsed < 5, f"工具评估接口耗时过长: {elapsed:.2f}s"
    assert result["status"] == "ok"
    assert result["mode"] == "tool"
    tool = result["data"]["results"]["tool"]
    assert tool["metrics"]["average_f1"] >= 0
    assert tool["test_count"] > 0

