# -*- coding: utf-8 -*-
"""下载并内置 ECharts 到 src/web/vendor/echarts.min.js

为什么必须本地内置：本系统定位为单机桌面程序，断网场景下不能依赖 CDN，
否则图表区域直接白屏。本脚本只需在首次部署时运行一次。

用法：
    uv run python scripts/fetch_echarts.py
    或 .venv\\Scripts\\python.exe scripts/fetch_echarts.py

若所在环境无法联网，可手动下载 echarts.min.js（任一 5.x 版本）放到
src/web/vendor/echarts.min.js，效果等同。前端在文件缺失时会自动降级为
表格展示，不会白屏，因此该步骤不阻塞系统运行。
"""
import sys
import urllib.request
from pathlib import Path

# 多个候选源：任一可用即成功（国内环境 npmmirror 通常最快）
SOURCES = [
    "https://registry.npmmirror.com/echarts/5.5.1/files/dist/echarts.min.js",
    "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
    "https://unpkg.com/echarts@5.5.1/dist/echarts.min.js",
]

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "web" / "vendor" / "echarts.min.js"
MIN_SIZE = 500 * 1024   # ECharts 完整包约 1MB，明显偏小说明下载到了错误内容


def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists() and TARGET.stat().st_size >= MIN_SIZE:
        print(f"[echarts] 已存在且大小正常，跳过下载: {TARGET} "
              f"({TARGET.stat().st_size // 1024} KB)")
        return 0

    for url in SOURCES:
        try:
            print(f"[echarts] 尝试下载: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if len(data) < MIN_SIZE:
                print(f"[echarts] 内容过小（{len(data)} 字节），换下一个源")
                continue
            TARGET.write_bytes(data)
            print(f"[echarts] 成功: {TARGET} ({len(data) // 1024} KB)")
            return 0
        except Exception as e:  # noqa: BLE001 - 逐个源尝试，全失败才提示手动下载
            print(f"[echarts] 失败: {e}")

    print("[echarts] 所有源均不可用。请手动下载 echarts.min.js（5.x）放到：")
    print(f"          {TARGET}")
    print("[echarts] 注意：缺少该文件不影响系统运行，图表会自动降级为表格展示。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
