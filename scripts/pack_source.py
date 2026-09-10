# -*- coding: utf-8 -*-
"""源码提交打包脚本（竞赛用）。

用法: python scripts/pack_source.py
产物: dist/audit-ai_v5.0GA_src_<时间戳>.zip

白名单策略：仅打包真正属于本项目的源码/配置/文档/测试；显式排除密钥(.env)、
口令库(*.db)、运行时产物、虚拟环境(.venv)、向量库(.chroma_db)、大安装包、
插件缓存(vercel-agent-skills/hallmark)、各类缓存目录。打包前断言无敏感文件。
"""
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 白名单：整目录纳入
WHITELIST_DIRS = ["src", "config", "knowledge_base", "assets", "tests", "scripts", "docs", "samples"]
# 白名单：单文件纳入
WHITELIST_FILES = [
    "pyproject.toml", "uv.lock", ".env.example", "Dockerfile", ".dockerignore",
    "README.md", "AGENTS.md", "DATA_SOURCES.md", "__init__.py",
    "start.ps1", "stop.ps1", "快速上手.txt",
    # 离线环境安装包（供无 Python/VC 运行库的机器直接安装，前置库安装.bat 会自动调用）
    "python-3.12.10-amd64.exe", "VC_redist.x64.exe",
]
# 目录内需剔除的缓存/产物目录名
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
# 目录内需剔除的文件（后缀/名）
SKIP_FILE_SUFFIX = (".pyc", ".log")
# local_storage 下的生成产物不打包（路径分段匹配：兼容 src/local_storage 与根 local_storage）
SKIP_STORAGE_SUBDIRS = {"reports", "charts"}
# 根目录 HTML 中要排除的（竞赛官方手册 / 娱乐向 / 副本）
HTML_EXCLUDE = re.compile(r"竞赛手册|baka|副本")
# 敏感文件断言：命中任一即中止
FORBIDDEN = re.compile(
    r"(^\.env$)|(\.db$)|(^checkpoints\.sqlite)|(\.sqlite)|"
    r"(^\.coverage$)|(authorship_secret)|(_real_run_result)|(^\.tmp_)|(_audit_msgs)"
)


def _is_skippable(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    if rel_path.endswith(SKIP_FILE_SUFFIX):
        return True
    # 任一层级出现 local_storage 且其下为 reports/charts 即剔除
    # （原 startswith 前缀匹配对 src/local_storage 永远不命中，导致真实运行产物泄漏进交付包）
    if "local_storage" in parts and any(p in SKIP_STORAGE_SUBDIRS for p in parts):
        return True
    return False


def collect_files() -> list:
    """按白名单收集待打包的相对路径列表。"""
    picked = []

    for d in WHITELIST_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(ROOT))
                if not _is_skippable(rel):
                    picked.append(rel)

    for f in WHITELIST_FILES:
        if (ROOT / f).exists():
            picked.append(f)

    # 根目录文档 HTML（排除官方手册/娱乐向）+ 启动 bat（中文名）
    for p in ROOT.glob("*.html"):
        if not HTML_EXCLUDE.search(p.name):
            picked.append(p.name)
    for p in ROOT.glob("*.bat"):
        picked.append(p.name)

    return sorted(set(picked))


def main():
    files = collect_files()

    # 敏感文件断言
    forbidden = [f for f in files if FORBIDDEN.search(Path(f).name)]
    if forbidden:
        print("[pack] 检测到敏感文件，已中止：", file=sys.stderr)
        for f in forbidden:
            print("  " + f, file=sys.stderr)
        sys.exit(1)

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    top = "audit-ai_v5.0GA_src"
    zip_path = dist / f"{top}_{stamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            # 归档内统一放到顶层目录下，解压即得一个整洁的项目文件夹
            zf.write(ROOT / rel, arcname=os.path.join(top, rel))

        # 打包后断言：运行产物（reports/charts）绝不允许出现在归档内
        leaked = [
            n for n in zf.namelist()
            if "local_storage" in n.replace("\\", "/").split("/")
            and any(seg in ("reports", "charts") for seg in n.replace("\\", "/").split("/"))
        ]
        if leaked:
            print("[pack] 归档内检测到运行产物，已中止：", file=sys.stderr)
            for n in leaked:
                print("  " + n, file=sys.stderr)
            sys.exit(1)

    size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
    print(f"[pack] 完成: {zip_path}")
    print(f"[pack] 文件数: {len(files)} | 大小: {size_mb} MB")


if __name__ == "__main__":
    main()
