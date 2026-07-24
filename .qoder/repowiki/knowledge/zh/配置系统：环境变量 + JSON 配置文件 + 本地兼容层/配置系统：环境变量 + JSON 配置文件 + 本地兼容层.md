---
kind: configuration_system
name: 配置系统：环境变量 + JSON 配置文件 + 本地兼容层
category: configuration_system
scope:
    - '**'
source_files:
    - config/agent_llm_config.json
    - src/local_shims.py
    - src/main.py
    - src/agents/agent.py
    - src/local_knowledge.py
    - scripts/load_env.py
    - .env.example
---

## 1. 采用的配置方式
本项目采用「环境变量 + JSON 配置文件」的轻量组合方案，没有引入集中式配置框架（如 pydantic-settings、dynaconf），而是通过 `os.getenv` 在各模块直接读取，辅以一个独立的 JSON 文件承载 Agent/LLM 运行时参数。

- **环境变量**：用于敏感信息（API Key）、路径与工作区定位、运行开关等。
- **JSON 配置文件**：存放 LLM 模型名称、温度、工具清单等可热更新的运行时参数。
- **本地兼容层**：在 `src/local_shims.py` 中集中暴露 `LOG_FILE`、`LOG_LEVEL`、`ENV` 等默认值，作为平台绑定层的统一入口。
- **脚本辅助加载**：`scripts/load_env.py` 使用 `python-dotenv` 从 `.env` 注入环境变量，供本地开发手动执行。

## 2. 关键文件与包
- `config/agent_llm_config.json` — LLM 模型、采样参数、Agent 角色提示词、工具白名单。
- `src/local_shims.py` — 本地兼容层，定义日志级别、环境判断、RunConfig 等默认值。
- `src/main.py` — FastAPI 入口，集中读取 `PORT`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`COZE_WORKSPACE_PATH` 等环境变量，并动态加载 JSON 配置。
- `src/agents/agent.py` — Agent 构建逻辑，读取 `REVIEW_ENABLED`、`REVIEW_MODEL`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`COZE_WORKSPACE_PATH`。
- `src/local_knowledge.py` — 知识库路径解析，依赖 `COZE_WORKSPACE_PATH`。
- `scripts/load_env.py` — 通过 `dotenv.load_dotenv` 从根目录 `.env` 加载环境变量。
- `.env.example` — 提供环境变量模板（二进制示例文件，实际内容见仓库）。 

## 3. 架构与约定
### 3.1 环境变量命名规范
| 变量名 | 用途 | 默认值 | 读取位置 |
|---|---|---|---|
| `OPENAI_API_KEY` | LLM API Key | 空串 | `main.py`, `agents/agent.py` |
| `OPENAI_BASE_URL` | LLM Base URL | `https://api.deepseek.com` | 同上 |
| `COZE_WORKSPACE_PATH` | 工作区根路径 | 当前脚本所在目录的父级 | `main.py`, `local_knowledge.py`, `agents/agent.py` |
| `PORT` | HTTP 服务端口 | `5000` | `main.py` `/api/status` |
| `ENV` | 运行环境标识 | `dev` | `local_shims.py` `graph_helper.is_dev_env()` |
| `LOG_LEVEL` | 日志级别 | `INFO` | `local_shims.py` |
| `REVIEW_ENABLED` / `REVIEW_MODEL` | 审查开关与模型 | `true` / `deepseek-chat` | `agents/agent.py` |

所有路径类变量均以 `COZE_WORKSPACE_PATH` 为基准拼接子目录（`knowledge_base/`、`config/`、`local_storage/`），保证容器化部署时只需挂载单一根目录。

### 3.2 JSON 配置文件结构
`config/agent_llm_config.json` 分为三段：
- `config.model` / `temperature` / `top_p` / `max_completion_tokens` / `timeout`：LLM 调用参数。
- `config.sp`：超长 System Prompt，包含审计规则、风险维度、输出 JSON Schema 等。
- `tools`：允许 Agent 调用的工具名白名单。

该文件由 `main.py` 的 `/api/status` 端点以只读方式加载，用于对外展示当前模型名；Agent 侧也通过 `graph_helper.get_agent_instance` 间接消费同一份配置。

### 3.3 启动流程中的配置装配
1. 进程启动 → `local_shims.setup_logging` 读取 `LOG_FILE`、`LOG_LEVEL`。
2. `main.py` 创建 FastAPI app，读取 `PORT`、`OPENAI_*`、`COZE_WORKSPACE_PATH`。
3. `lifespan` 钩子预加载 agent，agent 内部再读取 `REVIEW_*`、`OPENAI_*` 及 JSON 配置。
4. 知识库路径基于 `COZE_WORKSPACE_PATH/knowledge_base` 扫描 `.txt` 文件。

### 3.4 本地开发模式
开发者先执行 `python scripts/load_env.py` 将 `.env` 注入到进程环境，再启动 `uvicorn src.main:app --port 5000`。生产部署则直接在容器或宿主环境中设置对应环境变量。

## 4. 开发者应遵循的规则
1. **新增配置一律走环境变量**：敏感项（Key、URL）和路径类配置必须通过 `os.getenv` 读取，禁止硬编码。
2. **保持 `COZE_WORKSPACE_PATH` 为唯一根**：所有相对路径都应以此为基础拼接，避免多套路径约定。
3. **JSON 配置仅放“非敏感、可热更新”的参数**：模型名、温度、工具白名单等；不要放入密钥。
4. **新增环境变量需在 `.env.example` 补充注释**，并在 `main.py` 的 `/api/status` 中增加健康检查字段，便于运维观测。
5. **不要在业务模块内重复实现 dotenv 加载**：统一由 `scripts/load_env.py` 负责，业务代码只读 `os.environ`。
6. **修改 `config/agent_llm_config.json` 后无需重启**：Agent 每次构建实例时会重新读取，但需注意大段 System Prompt 变更对上下文窗口的影响。
