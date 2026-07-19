#!/bin/bash
set -eo pipefail

# ═══════════════════════════════════════════════════════════════
# 项目打包脚本 —— 生成可直接上传服务器的部署压缩包
# 用法: bash scripts/pack.sh              # 生成 tar.gz
#       bash scripts/pack.sh --docker     # 构建 Docker 镜像
# ═══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PACK_NAME="audit-ai_v3.0_${TIMESTAMP}.tar.gz"

cd "$PROJECT_DIR"

echo "[pack] 锁定依赖版本..."
uv lock

echo "[pack] 创建部署包: ${PACK_NAME}"

# 仅打包服务器需要的文件，排除 Windows 专用和开发产物
tar -czf "$PACK_NAME" \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='python-3.12.10-amd64.exe' \
    --exclude='VC_redist.x64.exe' \
    --exclude='*.ps1' \
    --exclude='*.bat' \
    --exclude='无用的readme' \
    --exclude='baka专用readme.html' \
    --exclude='local_storage/reports' \
    --exclude='local_storage/charts' \
    --exclude='app.log' \
    --exclude='.coze' \
    src/ \
    config/ \
    knowledge_base/ \
    assets/ \
    tests/ \
    scripts/setup.sh \
    pyproject.toml \
    uv.lock \
    .env.example \
    Dockerfile \
    README.md

echo "[pack] ✅ 完成: ${PACK_NAME}"
echo "[pack] 上传到服务器后执行: tar -xzf ${PACK_NAME} && bash scripts/setup.sh"
