---
kind: dependency_management
name: 基于 uv 的 Python 依赖管理与镜像源策略
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
---

本项目使用 uv（Rust 实现的现代 Python 包管理器）作为统一的依赖管理工具，通过 pyproject.toml 声明依赖、uv.lock 锁定精确版本与哈希，并以阿里云 PyPI 镜像为默认源。

## 1. 使用的系统与工具
- 包管理器：uv（替代 pip/pipenv/conda），支持快速解析、增量安装与跨平台 wheel 缓存。
- 元数据定义：pyproject.toml 中的 [project] 段声明运行时依赖，[dependency-groups.dev] 声明开发依赖。
- 锁文件：uv.lock 记录每个包的精确版本、来源 registry、sdist/wheel URL 及 sha256 哈希，保证构建可复现。
- Python 版本约束：requires-python = ">=3.10"，lock 中同时覆盖 3.10~3.14 多版本的 resolution-markers。

## 2. 关键文件
- pyproject.toml — 依赖声明、uv index 配置、pytest 路径。
- uv.lock — 全量依赖树与校验哈希。
- .env.example / scripts/load_env.* — 环境变量注入（LLM API Key 等）。
- Dockerfile — 容器化时复用 uv lock 进行安装。

## 3. 架构与约定
- 双索引源策略：
  - 默认索引指向阿里云镜像 https://mirrors.aliyun.com/pypi/simple/，加速国内下载。
  - 额外显式注册 pypi 索引（explicit = true），仅当某个包在 tool.uv.sources 中显式引用时才回退到官方 PyPI，用于解决镜像同步延迟导致的预发布版缺失问题。
- 版本范围约束：所有依赖采用 >=X,<Y 的宽松上限风格（如 langchain>=1.0,<2），在保持兼容性的同时允许小版本演进；对易出问题的原生扩展（如 chroma-hnswlib）则钉死到次版本内最小范围。
- 分组依赖：测试相关包放入 dependency-groups.dev，不进入生产环境。
- 无 vendoring：项目未将第三方库源码纳入仓库，完全依赖 uv 的虚拟环境与 lock 文件分发。

## 4. 开发者应遵循的规则
1. 新增依赖：在 pyproject.toml 的 dependencies 或 dev 组中添加条目，然后运行 uv lock --upgrade 生成新的 uv.lock，提交两者。
2. 不要手动编辑 uv.lock：其内容应由 uv 自动生成，包含完整的哈希校验。
3. 优先使用阿里云镜像：默认已配置，无需额外 --index-url；仅在个别包需要官方 PyPI 预发布版时，通过 tool.uv.sources 显式指定。
4. 保持 Python 版本 >=3.10：这是项目最低要求，升级需同步更新 requires-python 并重新 lock。
5. 避免引入裸版本号：始终给出 >=X,<Y 的范围，防止上游破坏性更新导致构建失败。