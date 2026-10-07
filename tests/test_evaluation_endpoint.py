"""评估接口回归：API 与命令行共用快速工具评估实现。"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_tool_evaluation_endpoint_returns_metrics_quickly(tmp_path):
    from main import run_evaluation

    started = time.perf_counter()
    # T5：测试必须把结果写进 tmp_path，禁止覆写版本库跟踪的 evaluation_results.json
    output_path = tmp_path / "evaluation_results.json"
    result = asyncio.run(run_evaluation("tool", output_path=str(output_path)))
    elapsed = time.perf_counter() - started

    assert elapsed < 5, f"工具评估接口耗时过长: {elapsed:.2f}s"
    assert result["status"] == "ok"
    assert result["mode"] == "tool"
    tool = result["data"]["results"]["tool"]
    assert tool["metrics"]["average_f1"] >= 0
    assert tool["test_count"] >= 20
    # 落盘文件带 provenance（git 版本戳），结果可追溯到代码版本
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted.get("provenance", {}).get("mode") == "tool"
    assert persisted["provenance"].get("git_head")

