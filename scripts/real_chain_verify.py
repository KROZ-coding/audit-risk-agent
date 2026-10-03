"""真实链路测试（含 LLM 全流程）——本地真实样本专用，数据不入库。

数据脱敏约定：被测年报路径通过命令行参数或环境变量 ``AUDIT_LOCAL_PDF``
提供（本地真实年报不入仓库）；脚本本身不含任何公司名或财务数据。

流程与前端完全一致：
1. pypdf 解析 PDF（同 /upload 逻辑，20 万字符截断）
2. 构造用户消息（文件名 + 全文，模拟前端拼接）
3. GraphService.stream_sse 驱动完整链路：P1 预处理并行注入（LLM 提取财务数据）
   → ReAct Agent（LLM 多轮工具调用）→ 多智能体辩论 → 兜底导出（PDF/Excel/图表）
   → 综合评分兜底 → final_report
4. 输出：ai_text 摘要、产物链接、工具链、耗时

用法:
    uv run python scripts/real_chain_verify.py <本地年报PDF路径>
    # 或：AUDIT_LOCAL_PDF=<路径> uv run python scripts/real_chain_verify.py
"""
import asyncio
import hashlib
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

PDF_PATH = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get("AUDIT_LOCAL_PDF", "")
)


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
    if not PDF_PATH or not os.path.isfile(PDF_PATH):
        print("未提供本地年报 PDF。用法：")
        print("  uv run python scripts/real_chain_verify.py <本地年报PDF路径>")
        print("  或设置环境变量 AUDIT_LOCAL_PDF（真实样本不入库）")
        return 1

    print("=" * 20, "步骤1：解析 PDF", "=" * 20)
    extracted = parse_pdf(PDF_PATH)
    print(f"解析完成：{len(extracted)} 字符")

    document_name = os.path.basename(PDF_PATH)
    instruction = "请对该公司年报进行全方位审计风险分析"
    content = (f"{instruction}\n\n（分析以下文件，第 1/1 份）：\n\n"
               f"📄 **文件: {document_name}**\n\n{extracted}")
    payload = {"messages": [{"role": "user", "content": content}]}
    with open(PDF_PATH, "rb") as source:
        source_hash = hashlib.sha256(source.read()).hexdigest()
    from pypdf import PdfReader
    payload["source_metadata"] = {
        "files": [{
            "document_name": document_name,
            "source_hash": source_hash,
            "page_count": len(PdfReader(PDF_PATH).pages),
        }]
    }

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
                return 1
    elapsed = time.time() - start
    print(f"链路完成：{elapsed:.0f} 秒 | 最后进度: {last_step}")

    if not report:
        print("未收到 final_report")
        return 1

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
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
