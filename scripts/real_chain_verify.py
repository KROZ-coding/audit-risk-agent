"""真实链路测试：中国石油 2025 半年报（含 LLM 全流程）。

流程与前端完全一致：
1. pypdf 解析 PDF（同 /upload 逻辑，20 万字符截断）
2. 构造用户消息（文件名 + 全文，模拟前端拼接）
3. GraphService.stream_sse 驱动完整链路：P1 预处理并行注入（LLM 提取财务数据）
   → ReAct Agent（LLM 多轮工具调用）→ 多智能体辩论 → 兜底导出（PDF/Excel/图表）
   → 综合评分兜底 → final_report
4. 输出：ai_text 摘要、产物链接、工具链、耗时

用法: uv run python scripts/real_chain_verify.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 加载 .env（与 scripts/load_env.py 一致）：build_agent 依赖 OPENAI_API_KEY 环境变量
_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

PDF_PATH = os.path.join(os.path.dirname(__file__), "..", "测试",
                        "中国石油：中国石油天然气股份有限公司2025 年半年度报告.pdf")


def parse_pdf(path: str) -> str:
    """与 /upload 一致的解析逻辑（pypdf + 20 万字符截断）。"""
    from pypdf import PdfReader
    reader = PdfReader(path)
    text_parts = []
    for i, page in enumerate(reader.pages):
        page_text = page.extract_text()
        if page_text:
            text_parts.append(f"--- 第 {i + 1} 页 ---\n{page_text}")
    text = "\n\n".join(text_parts)
    return text[:200_000]


async def main():
    print("=" * 20, "步骤1：解析 PDF", "=" * 20)
    extracted = parse_pdf(PDF_PATH)
    print(f"解析完成：{len(extracted)} 字符")

    instruction = "请对该公司年报进行全方位审计风险分析"
    content = (f"{instruction}\n\n（分析以下文件，第 1/1 份）：\n\n"
               f"📄 **文件: 中国石油：中国石油天然气股份有限公司2025 年半年度报告.pdf**\n\n{extracted}")
    payload = {"messages": [{"role": "user", "content": content}]}

    print("=" * 20, "步骤2：stream_sse 完整链路（含 LLM，预计 3-10 分钟）", "=" * 20)
    from main import GraphService
    svc = GraphService()
    start = time.time()
    report = None
    tool_names = []
    last_step = ""
    async for raw in svc.stream_sse(payload):
        for line in str(raw).splitlines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "progress":
                last_step = f"{ev.get('step', '')} {ev.get('percent', '')}%"
            elif ev.get("type") == "final_report":
                report = ev
            elif ev.get("type") == "error":
                print("链路错误:", ev.get("content"))
                return
    elapsed = time.time() - start
    print(f"链路完成：{elapsed:.0f} 秒 | 最后进度: {last_step}")

    if not report:
        print("未收到 final_report")
        return

    ai_text = str(report.get("ai_text", "") or "")
    print("=" * 20, "步骤3：产物清单", "=" * 20)
    files = report.get("files", []) or []
    images = report.get("images", []) or []
    print(f"文件 {len(files)} 个：")
    for f in files:
        print("  ", f.get("path"))
    print(f"图表 {len(images)} 个：")
    for i in images:
        print("  ", i.get("path"))
    print(f"工具链 {len(report.get('tool_results', []) or [])} 次调用：")
    for t in report.get("tool_results", []) or []:
        print("  -", t.get("name"))

    print("=" * 20, "步骤4：AI 正文摘要（前 2500 字）", "=" * 20)
    print(ai_text[:2500])
    print("...")
    print("正文总长:", len(ai_text))

    # 保存全文供比对
    out = os.path.join(os.path.dirname(__file__), "..", ".tmp_real_ai.txt")
    with open(out, "w", encoding="utf-8") as wf:
        wf.write(ai_text)
    print("正文已保存:", out)


if __name__ == "__main__":
    asyncio.run(main())
