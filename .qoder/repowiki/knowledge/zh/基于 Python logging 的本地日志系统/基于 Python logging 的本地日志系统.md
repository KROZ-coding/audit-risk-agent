---
kind: logging_system
name: 基于 Python logging 的本地日志系统
category: logging_system
scope:
    - '**'
source_files:
    - src/local_shims.py
    - src/main.py
---

## 1. 使用的系统与框架

本项目使用 **Python 标准库 `logging`** 作为唯一日志框架，未引入 loguru、structlog、log4j 等第三方方案。日志配置集中在兼容层模块中，由应用入口统一初始化。

## 2. 核心文件与包

- `src/local_shims.py`：日志系统的唯一配置中心，定义 `setup_logging()`、`LOG_FILE`、`LOG_LEVEL` 常量及 RotatingFileHandler 输出策略。
- `src/main.py`：FastAPI 服务入口，在进程启动时调用 `setup_logging()` 完成全局 logger 初始化，并持有模块级 `logger = logging.getLogger(__name__)`。
- 各业务模块（`src/agents/agent.py`、`src/local_knowledge.py`、`src/storage/**/*.py`、`src/tools/**/*.py`、`scripts/*.py`）均通过 `import logging; logger = logging.getLogger(__name__)` 获取子 logger。

## 3. 架构与约定

### 3.1 初始化流程
```text
main.py 启动
  → setup_logging(log_file=LOG_FILE, max_bytes=100MB, backup_count=5, log_level=LOG_LEVEL)
    → StreamHandler (控制台) + RotatingFileHandler (app.log 轮转)
    → logging.basicConfig(level=..., force=True) 覆盖默认配置
```
- 日志文件路径：`os.getcwd()/app.log`（可通过环境变量 `LOG_LEVEL` 控制级别，默认 `INFO`）
- 轮转策略：单文件最大 100 MB，保留最近 5 个备份，UTF-8 编码。

### 3.2 日志格式
采用固定模板：`%(asctime)s [%(levelname)s] %(name)s - %(message)s`，包含时间戳、级别、模块名和消息体，无结构化 JSON 字段。

### 3.3 日志级别使用约定
| 级别 | 使用场景 | 示例位置 |
|------|---------|----------|
| `info` | 业务流程关键节点（知识库加载、Agent 辩论步骤、文件上传成功、HTTP 请求开始） | `local_knowledge.py`、`agents/agent.py`、`main.py` |
| `warning` | 可恢复异常或降级路径（ChromaDB 失败回退 TF-IDF、PDF/Excel 导出兜底失败） | `local_knowledge.py`、`agents/agent.py` |
| `error` | 不可恢复错误（流处理器异常、知识库目录缺失） | `local_shims.py`、`scripts/init_knowledge_base.py` |
| `debug` | 未发现使用 | — |

### 3.4 上下文关联
每个 logger 以模块名为命名空间（`logging.getLogger(__name__)`），便于按模块过滤；同时通过 `ContextVar` 维护 `run_id`，但当前未在日志格式中注入该字段，仅通过 `logger.info(f"... run_id={run_id} ...")` 字符串拼接方式附带。

## 4. 开发者应遵循的规则

1. **统一入口**：不要在业务模块中重复调用 `basicConfig`，所有配置必须经由 `local_shims.setup_logging()` 完成。
2. **级别选择**：正常流程用 `info`，可降级场景用 `warning`，真正失败才用 `error`；避免滥用 `debug`（目前未启用）。
3. **run_id 追踪**：在 HTTP 请求处理链路中，通过 `ctx.run_id` 将请求标识拼入日志消息，便于跨模块关联排查。
4. **不记录敏感信息**：日志消息中不要写入用户隐私、API Key 等敏感内容；当前实现已遵循此原则。
5. **扩展建议**：如需结构化日志（JSON）、trace_id 自动注入、多 sink（ELK/Sentry），应在 `local_shims.setup_logging` 中集中改造，而非在各模块分散处理。

## 5. 已知局限

- 无结构化日志格式（JSON），不利于下游日志聚合分析。
- 未将 `run_id` 作为独立字段输出，仅嵌入消息文本，解析成本较高。
- 缺少统一的日志采集/上报机制（如 ELK、Sentry），仅落盘到本地 `app.log`。
- 未区分不同组件（Agent、Storage、Tools）的独立日志文件或 handler。
