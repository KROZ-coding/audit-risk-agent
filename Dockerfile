# ─── 上市公司年报风险识别 v3.0 ───
# 部署方式：docker build -t audit-ai . && docker run -p 5000:5000 audit-ai
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖 + uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# 先复制依赖文件（利用 Docker 层缓存）
COPY pyproject.toml uv.lock ./

# 安装 Python 依赖（--locked：严格按 uv.lock 安装，锁文件与 pyproject 不一致即失败）
RUN uv sync --locked --no-dev

# 复制项目文件
COPY src/ ./src/
COPY config/ ./config/
COPY knowledge_base/ ./knowledge_base/
COPY assets/ ./assets/
COPY tests/ ./tests/
# 密钥和运行配置不复制进镜像层，请在运行时通过环境变量或 --env-file 注入。

# ChromaDB 持久化目录（可挂载外部卷）
RUN mkdir -p /app/.chroma_db

# 预热嵌入模型（避免首次检索时联网下载超时）
# 注：chromadb 默认使用 ONNX 版 all-MiniLM-L6-v2，不依赖 sentence-transformers；
# 必须用 uv run 执行（依赖装在 uv 虚拟环境内，系统 python 无 chromadb）
RUN uv run python -c "from chromadb.utils import embedding_functions as ef; ef.DefaultEmbeddingFunction()(['warmup'])"

EXPOSE 5000

# 启动服务
CMD ["uv", "run", "python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "5000"]
