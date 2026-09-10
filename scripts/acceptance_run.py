# -*- coding: utf-8 -*-
"""P6 端到端验收：驱动 /stream_run 跑一次单模块分析，落盘 SSE 全文与最终报告。

用法：python scripts/acceptance_run.py <module> <公司名> [输出前缀]
module 取 financial / compliance / synthesis
"""
import json
import sys
import time
import urllib.request

MODULE_MARKERS = {
    "financial": "【模块:财务健康度诊断】",
    "compliance": "【模块:合规与经营风险扫描】",
    "synthesis": "【模块:综合研判】",
}

module = sys.argv[1] if len(sys.argv) > 1 else "financial"
company = sys.argv[2] if len(sys.argv) > 2 else "贵州茅台"
prefix = sys.argv[3] if len(sys.argv) > 3 else f"_acc_{module}"

marker = MODULE_MARKERS[module]
text = f"{marker}请分析 {company} 最近五年的年报风险，覆盖 2020-2024 年度数据。"
payload = {"messages": [{"role": "user", "content": text}]}

req = urllib.request.Request(
    "http://127.0.0.1:5000/stream_run",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)

t0 = time.time()
raw_lines = []
progress = []
final_text = ""
tools = []
# 边收边落盘：长耗时串跑中途掉线时不丢已收到的 SSE（实测过最后一帧被重置）
sink = open(f"{prefix}_sse.txt", "w", encoding="utf-8")
try:
    with urllib.request.urlopen(req, timeout=900) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            raw_lines.append(line)
            sink.write(line + "\n")
            sink.flush()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            t = ev.get("type")
            if t == "progress":
                progress.append((ev.get("percent"), ev.get("step")))
                print(f"  [{ev.get('percent'):>3}%] {ev.get('step')}", flush=True)
            elif t == "tool":
                tools.append(ev.get("name") or ev.get("tool"))
            elif t == "final_report":
                final_text = ev.get("ai_text") or ""
except Exception as e:  # noqa: BLE001 - 验收脚本：掉线也要保留已收集的证据
    print("!! stream interrupted:", type(e).__name__, e)
finally:
    sink.close()

elapsed = time.time() - t0
with open(f"{prefix}_report.md", "w", encoding="utf-8") as f:
    f.write(final_text)

print(f"\n=== module={module} elapsed={elapsed:.1f}s ===")
print("progress anchors:", len(progress))
print("tools:", tools)
print("report chars:", len(final_text))
