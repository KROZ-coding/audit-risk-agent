---
kind: error_handling
name: 错误处理策略：基于 FastAPI HTTPException 与日志兜底，无统一异常体系
category: error_handling
scope:
    - '**'
source_files:
    - src/main.py
    - src/local_shims.py
    - src/agents/agent.py
    - src/tools/data_validator.py
---

## 1. 采用的系统/方法
- **HTTP 层**：使用 FastAPI 的 `HTTPException` 作为统一的 API 错误返回方式（400/500），由路由函数在解析失败、超时等场景主动抛出。
- **流式 SSE 层**：`stream_sse` 中捕获 `Exception` 后以 `{type: "error", content: str(e)}` 事件形式推送给前端，不中断流。
- **异步任务层**：`GraphService.run` 对 `asyncio.CancelledError` 单独处理并返回 `{status: "cancelled"}`；其他异常记录堆栈后重新抛出，交由上层 HTTP 层处理。
- **工具/Agent 层**：大量使用 `try/except Exception as e` + `logger.warning/logger.error` 做“吞错+降级”，例如 `_AgentWrapper._post_process` 中对 PDF/Excel 导出失败仅打 warning 并继续执行后续逻辑；辩论机制失败返回 None，不影响主报告生成。
- **本地兼容层**：`src/local_shims.py` 提供最小化的 `ErrorClassifier` / `ClassifiedError` 占位实现，仅返回 code=500 和原始消息字符串，未参与实际分类逻辑。
- **日志系统**：通过 `local_shims.setup_logging` 配置 RotatingFileHandler 写入 `app.log`，所有关键路径均记录 `logger.error/warning/info`，但无结构化日志字段或告警通道。
- **无全局异常中间件**：FastAPI 应用未注册自定义 exception handler，依赖框架默认行为将未捕获异常转为 500 JSON 响应。
- **无 panic/recover 模式**：Python 生态下未见 `try/finally` 之外的恢复语义，也未见 `sys.exit()` 或进程级重启逻辑。

## 2. 关键文件与位置
- `src/main.py`：HTTP 路由层，集中出现 `raise HTTPException(...)`、`asyncio.TimeoutError` 处理、SSE error 事件推送。
- `src/local_shims.py`：`ErrorClassifier`、`ClassifiedError`、`AsyncTaskStorageError` 占位定义；`setup_logging` 日志初始化。
- `src/agents/agent.py`：`_AgentWrapper._post_process` 中多处 `except Exception` 降级处理，确保导出/辩论失败不影响主流程。
- `src/tools/data_validator.py`：工具内部用 `_safe_float` 防御脏数据，失败时返回默认值而非抛异常，体现“容错优先”风格。

## 3. 架构与约定
- **分层职责清晰**：HTTP 层负责对外错误码映射；Agent/工具层负责内部健壮性（吞错+降级）；日志贯穿各层用于可观测性。
- **SSE 与同步接口差异化**：同步 `/run` 直接 raise HTTPException；流式 `/stream_run` 通过事件推送 error，避免客户端连接中断。
- **Agent 输出兜底**：即使 LLM 遗漏导出工具调用，包装器也会尝试补调；若失败仅记录 warning，保证用户至少拿到文本结论。
- **无业务异常类型体系**：未发现自定义业务 Error 类（如 `ValidationError`、`KnowledgeBaseError` 等），所有业务异常均以通用 `Exception` 表达。

## 4. 开发者应遵循的规则
1. **HTTP 入口统一抛 `HTTPException`**：参数校验失败 → 400；运行时异常 → 500，并在 `detail` 中包含可读信息。
2. **流式接口用 SSE error 事件**：在 `stream_sse` 的 `except Exception` 分支中 yield `{type: "error", ...}`，不要 raise 导致连接断开。
3. **工具/Agent 内部尽量吞错降级**：使用 `try/except Exception` + `logger.warning`，返回空结果或默认值，避免阻断主链路。
4. **敏感错误只记日志不暴露细节**：`logger.error` 记录完整 traceback，但 HTTP `detail` 中仅返回脱敏后的 `str(e)`。
5. **新增业务异常需先扩展 `ErrorClassifier`**：当前分类器为占位实现，未来如需区分错误类别，应在 `local_shims.ErrorClassifier.classify` 中补充规则。
6. **避免在工具函数中 raise 未声明异常**：LLM 可能无法理解 Python 异常语义，建议返回结构化错误 JSON 并由上层转换。