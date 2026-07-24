---
kind: build_system
name: 构建与部署体系（uv + Docker + Shell/PowerShell 脚本）
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - Dockerfile
    - scripts/pack.sh
    - scripts/setup.sh
    - scripts/http_run.sh
    - scripts/local_run.sh
    - start.ps1
    - .env.example
---

## 1. 使用的工具链与整体思路
- 包管理与依赖锁定：使用 `uv`（Astral）作为 Python 包管理器，通过 `pyproject.toml` 声明依赖、`uv.lock` 锁定精确版本，保证本地与 CI/容器内安装结果一致。
- 镜像构建：提供 `Dockerfile`，基于 `python:3.12-slim`，在镜像中预装 uv 并执行 `uv sync --frozen`，实现可复现的容器化运行环境。
- 启动与打包：通过 `scripts/` 下的 shell 和 PowerShell 脚本统一入口，覆盖开发、打包、HTTP 服务启动等场景；Windows 侧提供 `start.ps1` 一键引导。
- 仓库镜像源：`pyproject.toml` 中将阿里云 PyPI 设为默认 index，并在需要时回退到官方 pypi.org，解决国内网络与预发布包同步延迟问题。

## 2. 关键文件与职责
- `pyproject.toml`：项目元信息（name/version/description）、Python 版本约束（>=3.10）、所有运行时依赖、uv index 配置、dev 依赖组、pytest 路径设置。
- `uv.lock`：由 `uv lock` 生成的完整依赖快照，包含多平台 wheel 与哈希值，是 `--frozen` 模式安装的可信来源。
- `Dockerfile`：定义容器镜像构建流程，包括系统依赖、uv 安装、依赖层缓存优化、数据目录创建、嵌入模型预下载、端口暴露与服务启动命令。
- `scripts/pack.sh`：生成服务器部署压缩包，先 `uv lock` 再 `tar -czf` 仅打包生产所需目录，排除 Windows 专用文件、日志与本地输出。
- `scripts/setup.sh`：初始化脚本，区分“deploy 模式”（通过 `PIP_TARGET` 导出到指定目录）与“devbox 模式”（写入 `.venv`），均基于 `uv` 完成依赖安装。
- `scripts/http_run.sh`：HTTP 服务启动器，自动激活 `.venv`（若存在），以 `src/main.py -m http -p <port>` 方式启动 FastAPI 服务。
- `scripts/local_run.sh`：通用 CLI 启动器，支持 `-m flow/node/agent/http` 多种模式，并通过 `load_env.sh` 加载环境变量。
- `start.ps1`：Windows 一键启动脚本，负责 Python 检查、`.env` 加载、uv 依赖同步、知识库与字体校验、按模式启动 HTTP/Web/CLI，并提供友好的错误提示。
- `.env.example`：环境变量模板（如 `OPENAI_API_KEY`、`COZE_WORKSPACE_PATH`、`LOG_LEVEL`、`ENV` 等）。

## 3. 架构与约定
- 依赖管理约定
  - 所有依赖变更必须通过 `uv lock` 更新 `uv.lock`，禁止手动编辑锁文件。
  - 生产安装一律使用 `--frozen` 模式，确保与锁文件完全一致。
  - 默认使用阿里云 PyPI 镜像，仅在个别包显式指定 `explicit=true` 的 index 时才走官方源。
- 运行环境约定
  - Python 版本要求 >=3.10，推荐 3.12（Docker 基础镜像即为 3.12）。
  - 开发环境优先使用 uv 创建的 `.venv`；容器环境则直接在镜像内安装依赖。
  - 敏感配置通过 `.env` 注入，不提交真实密钥。
- 启动入口约定
  - 所有运行模式最终都调用 `src/main.py`，通过 `-m` 参数选择 `http/flow/node/agent` 四种模式。
  - HTTP 服务默认监听 5000 端口，可通过环境变量 `DEPLOY_RUN_PORT` 或脚本参数覆盖。
- 产物与数据目录约定
  - 图表与报告输出到 `local_storage/charts` 与 `local_storage/reports`，打包脚本会排除这些目录以避免体积膨胀。
  - ChromaDB 向量库持久化目录为 `.chroma_db`，Docker 中已创建并可外部挂载。
- 测试约定
  - 使用 pytest，`pyproject.toml` 中已将 `src` 加入 `pythonpath`，可直接 import 项目模块进行单元测试。

## 4. 开发者应遵循的规则
- 新增依赖后务必执行 `uv lock` 并提交 `uv.lock`，否则 CI/容器构建可能失败。
- 修改 `pyproject.toml` 中的依赖范围时，注意保持与现有 `uv.lock` 兼容，避免破坏 `--frozen` 安装。
- 本地调试优先使用 `start.ps1`（Windows）或 `scripts/local_run.sh`（Linux/macOS），不要直接裸调 `python src/main.py`，以免丢失环境变量与 venv 激活逻辑。
- 生产部署推荐使用 `scripts/pack.sh` 生成 tar.gz 包，或在 CI 中直接 `docker build` 构建镜像；两者都依赖 `uv.lock` 保证一致性。
- 如需自定义镜像行为，请优先修改 `Dockerfile`，而非在运行时动态 pip install，以保持镜像可重复性。
- 环境变量通过 `.env` 管理，新增变量需同步更新 `.env.example` 并在 `start.ps1` / `scripts/load_env.sh` 中做好提示或校验。