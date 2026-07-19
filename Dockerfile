# ─── 上市公司年报审计风险识别系统 v3.0 ───
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

# 安装 Python 依赖
RUN uv sync --frozen --no-dev

# 复制项目文件
COPY src/ ./src/
COPY config/ ./config/
COPY knowledge_base/ ./knowledge_base/
COPY assets/ ./assets/
COPY tests/ ./tests/
COPY .env ./

# ChromaDB 持久化目录（可挂载外部卷）
RUN mkdir -p /app/.chroma_db

# 预下载嵌入模型（避免首次调用时下载超时）
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

EXPOSE 5000

# 启动服务
CMD ["uv", "run", "python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "5000"]
